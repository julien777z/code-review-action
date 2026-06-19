import asyncio
import json
import logging
import signal
import subprocess
from collections.abc import Awaitable, Callable
from fnmatch import fnmatch

from pydantic import ValidationError

from code_review.config import SETTINGS
from code_review.github import (
    already_reviewed,
    complete_check_run,
    current_head_sha,
    diff_anchors,
    head_check_concluded,
    list_review_threads,
    post_review,
    pull_request_diff,
    resolve_threads,
    start_check_run,
)
from code_review.models.shared.findings import Finding, ReviewComment, ReviewPayload
from code_review.models.shared.pull_request import PostedFinding, PullRequestContext, ReviewInputs
from code_review.models.shared.severity import DiffSide, Severity
from code_review.models.shared.threads import ThreadCommentNode

logger = logging.getLogger("code_review.review")

GetFindings = Callable[[ReviewInputs], Awaitable[list[Finding]]]


class ReviewBackendError(Exception):
    """A backend failed to produce findings (model error or unparseable reply)."""


def thread_title(comment: ThreadCommentNode) -> str | None:
    """Return the finding title from this tier's comment body (the `### ` heading), if present."""

    return next((row[4:].strip() for row in comment.body.splitlines() if row.startswith("### ")), None)


def thread_severity(comment: ThreadCommentNode) -> Severity | None:
    """Return the severity from this tier's comment body (the `**X Severity**` line), if present."""

    line = next(
        (
            row.strip()
            for row in comment.body.splitlines()
            if row.strip().startswith("**") and row.strip().lower().endswith("severity**")
        ),
        "",
    )
    words = line.strip("*").split()
    if not words:
        return None

    try:
        return Severity.from_str(words[0])
    except ValueError:
        return None


def is_tier_comment(comment: ThreadCommentNode | None, marker: str) -> bool:
    """Return True when the comment is the runner's own posting (github-actions bot plus the marker)."""

    if comment is None:
        return False

    author = comment.author.login if comment.author else None

    return author in ("github-actions", "github-actions[bot]") and marker in comment.body


async def existing_finding_titles(repo: str, pr_number: int, marker: str) -> dict[str, list[PostedFinding]]:
    """Return the runner's posted (severity, title) pairs per file (open and resolved); best-effort."""

    try:
        threads = await list_review_threads(repo, pr_number)
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError, TypeError, ValidationError):
        return {}

    findings: dict[str, list[PostedFinding]] = {}
    for thread in threads:
        comment = next(iter(thread.comments.nodes), None)
        if not is_tier_comment(comment, marker) or comment is None:
            continue

        title = thread_title(comment)
        if comment.path and title:
            severity = thread_severity(comment)
            findings.setdefault(comment.path, []).append(
                PostedFinding(severity=severity.value if severity else "", title=title)
            )

    return findings


async def reconcile_threads(
    repo: str,
    pr_number: int,
    marker: str,
    current_keys: set[tuple[str, str]],
    reviewed_files: set[str],
) -> tuple[set[tuple[str, str]], set[tuple[str, str]], list[str], set[tuple[str, str]]]:
    """Classify the runner's threads read-only into posted, open, stale, and kept-blocking keys."""

    threads = await list_review_threads(repo, pr_number)

    posted_keys: set[tuple[str, str]] = set()
    open_keys: set[tuple[str, str]] = set()
    stale_ids: list[str] = []
    kept_blocking_keys: set[tuple[str, str]] = set()

    for thread in threads:
        comment = next(iter(thread.comments.nodes), None)
        if not is_tier_comment(comment, marker) or comment is None:
            continue

        title = thread_title(comment)
        if title is None:
            continue

        key = (comment.path or "", title)
        posted_keys.add(key)
        if thread.is_resolved:
            continue

        if key in current_keys:
            open_keys.add(key)

            continue

        # Gone this round: resolve only if outdated, or non-blocking on a re-reviewed file (the agent
        # re-reports only on changed lines). Keep blocking threads and threads on unreviewed files open.
        severity = thread_severity(comment)
        is_blocking = severity is not None and severity in SETTINGS.approval_include

        if thread.is_outdated or (comment.path in reviewed_files and not is_blocking):
            stale_ids.append(thread.id)
        else:
            open_keys.add(key)
            if is_blocking:
                kept_blocking_keys.add(key)

    return posted_keys, open_keys, stale_ids, kept_blocking_keys


def path_allowed(path: str) -> bool:
    """Return whether a path passes the include/exclude glob filters."""

    if SETTINGS.include_paths and not any(fnmatch(path, glob) for glob in SETTINGS.include_paths):
        return False

    return not any(fnmatch(path, glob) for glob in SETTINGS.exclude_paths)


def filter_findings(findings: list[Finding]) -> list[Finding]:
    """Drop findings below the severity threshold or outside the path filters."""

    return [
        finding
        for finding in findings
        if finding.severity.meets(SETTINGS.min_severity) and path_allowed(finding.path)
    ]


def dedupe_findings(findings: list[Finding]) -> list[Finding]:
    """Drop repeat findings sharing a path, line, side, and title."""

    seen: set[tuple[str, int, DiffSide, str]] = set()
    deduped: list[Finding] = []

    for finding in findings:
        key = (finding.path, finding.line, finding.side, finding.title.strip())
        if key in seen:
            continue

        seen.add(key)
        deduped.append(finding)

    return deduped


def cap_findings(findings: list[Finding]) -> list[Finding]:
    """Keep findings most-important-first under the Low and total caps."""

    capped: list[Finding] = []
    low_count = 0

    for finding in findings:
        if finding.severity is Severity.LOW:
            if low_count >= SETTINGS.low_findings_cap:
                continue

            low_count += 1

        capped.append(finding)
        if SETTINGS.max_findings is not None and len(capped) >= SETTINGS.max_findings:
            break

    return capped


def comment_body(finding: Finding, marker: str) -> str:
    """Render one inline comment body in the severity format."""

    return (
        f"### {finding.title}\n\n**{finding.severity.value.capitalize()} Severity**\n\n"
        f"{finding.body}\n\n{marker}"
    )


def finding_anchors(finding: Finding, anchors: dict[str, tuple[set[int], set[int]]]) -> bool:
    """Return True if the finding's line is present on its diff side."""

    right, left = anchors.get(finding.path, (set(), set()))

    return finding.line in (left if finding.side is DiffSide.LEFT else right)


def is_postable(
    finding: Finding, anchors: dict[str, tuple[set[int], set[int]]], unpatched: set[str]
) -> bool:
    """Return True if the finding can be posted: inline-anchorable, or on a changed file with no patch."""

    return finding_anchors(finding, anchors) or finding.path in unpatched


def compute_verdict(open_count: int, open_blocking: bool) -> tuple[str, str, str]:
    """Return the (review event, check conclusion, check title) for the round's open-issue state."""

    if open_count == 0:
        return "APPROVE", "success", "No unresolved issues"

    if open_blocking:
        return "REQUEST_CHANGES", "failure", "Blocking issue open"

    plural = "s" if open_count != 1 else ""

    return "COMMENT", "neutral", f"{open_count} unresolved issue{plural}"


def verdict_summary(event: str, open_count: int, previous_count: int) -> str:
    """Phrase the verdict as the count of unresolved issues and how many carried from past reviews."""

    if event == "APPROVE":
        return "No unresolved issues — approving."

    plural = "s" if open_count != 1 else ""
    verb = "is" if open_count == 1 else "are"
    carried = f" (including {previous_count} from a previous review)" if previous_count else ""
    line = f"There {verb} {open_count} unresolved issue{plural}{carried}."

    if event == "REQUEST_CHANGES":
        return f"{line} A blocking issue is open — requesting changes."

    return line


def build_review(
    head_sha: str,
    findings: list[Finding],
    anchors: dict[str, tuple[set[int], set[int]]],
    event: str,
    summary_line: str,
    marker: str,
) -> ReviewPayload:
    """Build the round's review: inline comments for the new findings plus the verdict summary body."""

    comments: list[ReviewComment] = []
    summary: list[str] = []

    for finding in findings:
        if finding_anchors(finding, anchors):
            comments.append(
                ReviewComment(
                    path=finding.path,
                    line=finding.line,
                    side=finding.side,
                    body=comment_body(finding, marker),
                )
            )
        else:
            summary.append(
                f"- {finding.path}:{finding.line} — {finding.severity.value.capitalize()} — {finding.body}"
            )

    body = summary_line
    if summary:
        body = f"{body}\n\nOn files too large to anchor inline:\n" + "\n".join(summary)

    body = f"{body}\n\n{marker}"

    return ReviewPayload(commit_id=head_sha, event=event, body=body, comments=comments)


async def post_review_with_fallback(repo: str, pr_number: int, payload: ReviewPayload, event: str) -> None:
    """Post the review, re-posting an APPROVE the bot cannot submit as a COMMENT."""

    if await post_review(repo, pr_number, payload):
        return

    # github-actions[bot] cannot APPROVE a PR, so re-post a clean verdict as a COMMENT (the check
    # run stays the real verdict).
    if event == "APPROVE":
        payload.event = "COMMENT"

    if event != "APPROVE" or not await post_review(repo, pr_number, payload):
        logger.warning("Could not post the %s review; the check run still records the verdict.", event)


async def run_review_round(pr: PullRequestContext, marker: str, get_findings: GetFindings) -> int:
    """Gather inputs, run a backend to get findings, then reconcile, post, and record the verdict."""

    # The verdict of record is the check run (approval on) or the review marker (approval off).
    already = (
        await already_reviewed(pr.repo, pr.number, pr.head_sha, marker)
        if SETTINGS.approval_disable
        else await head_check_concluded(pr.repo, pr.head_sha)
    )
    if already:
        logger.info("Head %s already reviewed; skipping.", pr.head_sha)

        return 0

    diff, (anchors, unpatched), posted_findings = await asyncio.gather(
        pull_request_diff(pr.repo, pr.number),
        diff_anchors(pr.repo, pr.number),
        existing_finding_titles(pr.repo, pr.number, marker),
    )
    inputs = ReviewInputs(pr=pr, diff=diff, posted_findings=posted_findings)

    check_id = None if SETTINGS.approval_disable else await start_check_run(pr.repo, pr.head_sha)
    concluded = False
    loop = asyncio.get_running_loop()
    review_task = asyncio.current_task()

    # cancel-in-progress would leave this head's check stuck in_progress; conclude it on cancellation.
    def _cancel_on_signal() -> None:
        """Cancel the in-flight review so the check run concludes when the job is cancelled."""

        if review_task is not None:
            review_task.cancel()

    for cancel_signal in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(cancel_signal, _cancel_on_signal)

    try:
        try:
            findings = await get_findings(inputs)
        except ReviewBackendError as exc:
            logger.error("Review backend failed: %s", exc)
            await complete_check_run(pr.repo, check_id, "action_required", "Review failed", str(exc))
            concluded = True

            return 1

        findings = filter_findings(dedupe_findings(findings))
        current_keys = {(finding.path, finding.title.strip()) for finding in findings}

        # Re-gate: never anchor a review to a head that advanced while the backend ran.
        if await current_head_sha(pr.repo, pr.number) != pr.head_sha:
            logger.info("Head moved during review; skipping (the new commit reviews next).")
            await complete_check_run(pr.repo, check_id, "cancelled", "Superseded", "The head moved during review.")
            concluded = True

            return 0

        reviewed_files = set(anchors) | unpatched
        posted_keys, open_existing, stale_ids, kept_blocking = await reconcile_threads(
            pr.repo, pr.number, marker, current_keys, reviewed_files
        )

        postable = [finding for finding in findings if is_postable(finding, anchors, unpatched)]
        new_findings: list[Finding] = []
        seen_new_keys: set[tuple[str, str]] = set()
        for finding in postable:
            key = (finding.path, finding.title.strip())
            if key in posted_keys or key in seen_new_keys:
                continue

            seen_new_keys.add(key)
            new_findings.append(finding)

        new_findings = cap_findings(new_findings)

        severity_by_key = {(f.path, f.title.strip()): f.severity for f in findings}
        new_open_keys = {key for key in current_keys if key not in posted_keys}
        open_keys = open_existing | new_open_keys
        open_count = len(open_keys)
        open_blocking = bool(kept_blocking) or any(
            severity_by_key.get(key) in SETTINGS.approval_include for key in open_keys
        )

        previous_count = len(open_existing)
        if SETTINGS.approval_disable:
            event, conclusion, title = "COMMENT", "neutral", ""
        else:
            event, conclusion, title = compute_verdict(open_count, open_blocking)

        summary = verdict_summary(event, open_count, previous_count)

        # Re-check the head right before mutating: it can advance during reconciliation.
        if await current_head_sha(pr.repo, pr.number) != pr.head_sha:
            logger.info("Head moved before posting; skipping (the new commit reviews next).")
            await complete_check_run(pr.repo, check_id, "cancelled", "Superseded", "The head moved before posting.")
            concluded = True

            return 0

        # Post only when there are new comments; otherwise the check run carries the verdict.
        if new_findings and not await already_reviewed(pr.repo, pr.number, pr.head_sha, marker):
            payload = build_review(pr.head_sha, new_findings, anchors, event, summary, marker)
            await post_review_with_fallback(pr.repo, pr.number, payload, event)

        logger.info("Resolving %d stale thread(s); %d open issue(s) remain.", len(stale_ids), open_count)
        await resolve_threads(pr.repo, stale_ids)

        await complete_check_run(pr.repo, check_id, conclusion, title, summary)
        concluded = True

        return 0
    except asyncio.CancelledError:
        await complete_check_run(pr.repo, check_id, "cancelled", "Superseded", "The review job was cancelled.")
        concluded = True

        return 1
    finally:
        for cancel_signal in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(cancel_signal)

        if not concluded:
            await complete_check_run(
                pr.repo, check_id, "action_required", "Review failed", "The review run did not complete."
            )
