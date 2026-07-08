import asyncio
import logging
import signal
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from fnmatch import fnmatch
from typing import Final

from pydantic import BaseModel

from code_review.config import CONFIG, DISCLAIMER, SETTINGS
from code_review.github import (
    already_reviewed,
    complete_check_run,
    current_head_sha,
    diff_anchors,
    head_check_concluded,
    list_review_threads,
    post_comment,
    post_review,
    pull_request_diff_if_available,
    resolve_threads,
    start_check_run,
)
from code_review.models.shared.findings import Finding, ReviewCommentRequest, ReviewPayload
from code_review.models.shared.pull_request import PostedFinding, PullRequestContext, ReviewInputs
from code_review.models.shared.severity import DiffSide, Severity
from code_review.models.shared.threads import ReviewThread, ThreadCommentNode

logger = logging.getLogger("code_review.review")

GetFindings = Callable[[ReviewInputs], AsyncIterator[Finding]]

REVIEW_BACKEND_ATTEMPTS: Final[int] = 3
REVIEW_RETRY_BACKOFF: Final[timedelta] = timedelta(seconds=2)


class ReviewRoundResult(BaseModel):
    """The outcome of one review round and the diff snapshot it reviewed."""

    exit_code: int
    diff: str | None = None


class ReviewBackendError(Exception):
    """A backend failed to produce findings (model error or unparseable reply)."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


async def stream_findings_with_retry(
    get_findings: GetFindings, inputs: ReviewInputs
) -> AsyncIterator[Finding]:
    """Stream the backend's findings, retrying transient failures only before the first one arrives."""

    for attempt in range(REVIEW_BACKEND_ATTEMPTS):
        produced = False
        try:
            async for finding in get_findings(inputs):
                produced = True
                yield finding

            return
        except ReviewBackendError as exc:
            if produced or not exc.retryable or attempt == REVIEW_BACKEND_ATTEMPTS - 1:
                raise

            backoff = REVIEW_RETRY_BACKOFF * (2**attempt)
            logger.warning("Review backend failed; retrying in %ss: %s", backoff.total_seconds(), exc)

            await asyncio.sleep(backoff.total_seconds())


def thread_title(comment: ThreadCommentNode) -> str | None:
    """Return the finding title from this tier's comment body (the `### ` heading), if present."""

    return next((row[4:].strip() for row in comment.body.splitlines() if row.startswith("### ")), None)


def thread_severity(comment: ThreadCommentNode) -> Severity | None:
    """Return the severity from this tier's current fixed severity line."""

    split_body = comment.body.split(CONFIG["untrusted_input_open"], 1)
    if len(split_body) != 2:
        return None

    fenced_body = split_body[1].split(CONFIG["untrusted_input_close"], 1)[0]
    lines = [row.strip() for row in fenced_body.splitlines()]
    try:
        heading_index = next(index for index, row in enumerate(lines) if row.startswith("### "))
    except StopIteration:
        return None

    line = next((row for row in lines[heading_index + 1 :] if row), "")
    severity_line = line.split("<br>", 1)[0]
    if not severity_line.startswith("**") or not severity_line.endswith(" Severity**"):
        return None

    severity_text = severity_line.removeprefix("**").removesuffix(" Severity**")
    try:
        return Severity.from_str(severity_text)
    except ValueError:
        return None


def finding_severity_line(finding: Finding) -> str:
    """Return the prominent severity line shown below the finding title."""

    return f"**{finding.severity.value.capitalize()} Severity**"


def finding_category_footer(finding: Finding) -> str:
    """Return the small category footer for review comments."""

    return f"<sub>{finding.category.label}</sub>"


def is_tier_comment(comment: ThreadCommentNode | None, marker: str) -> bool:
    """Return True when the comment is the runner's own posting, identified by the marker."""

    if comment is None:
        return False

    # Match on the marker alone, not the author: the action may post as github-actions[bot], a GitHub
    # App, or a PAT user, but every comment it writes carries the marker.
    return marker in comment.body


async def existing_finding_titles(repo: str, pr_number: int, marker: str) -> dict[str, list[PostedFinding]]:
    """Return the runner's posted (severity, title) pairs per file (open and resolved)."""

    threads = await list_review_threads(repo, pr_number)
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


def extract_posted_keys(threads: list[ReviewThread], marker: str) -> set[tuple[str, str]]:
    """Return every (path, title) the runner has already posted, open or resolved."""

    keys: set[tuple[str, str]] = set()

    for thread in threads:
        comment = next(iter(thread.comments.nodes), None)
        if not is_tier_comment(comment, marker) or comment is None:
            continue

        title = thread_title(comment)
        if title is None:
            continue

        keys.add((comment.path or "", title))

    return keys


def classify_threads(
    threads: list[ReviewThread],
    marker: str,
    current_keys: set[tuple[str, str]],
    reviewed_files: set[str],
) -> tuple[set[tuple[str, str]], list[str], set[tuple[str, str]]]:
    """Split the runner's threads into still-open, stale (to resolve), and kept-blocking keys."""

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

    return open_keys, stale_ids, kept_blocking_keys


def path_allowed(path: str) -> bool:
    """Return whether a path passes the include/exclude glob filters."""

    if SETTINGS.include_paths and not any(fnmatch(path, glob) for glob in SETTINGS.include_paths):
        return False

    return not any(fnmatch(path, glob) for glob in SETTINGS.exclude_paths)


def finding_kept(finding: Finding) -> bool:
    """Return True when a finding passes the severity bar and the path filters."""

    return finding.severity.meets(SETTINGS.min_severity) and path_allowed(finding.path)


def cap_decision(finding: Finding, low_count: int, total_count: int) -> bool:
    """Return whether to post a finding given the running Low and total caps."""

    if SETTINGS.max_findings is not None and total_count >= SETTINGS.max_findings:
        return False

    if finding.severity is Severity.LOW and low_count >= SETTINGS.low_findings_cap:
        return False

    return True


def comment_body(finding: Finding, marker: str) -> str:
    """Render one inline comment body with category and severity."""

    return (
        f"{CONFIG['untrusted_input_open']}\n"
        f"### {finding.title}\n\n{finding_severity_line(finding)}<br>{finding_category_footer(finding)}\n\n{finding.body}\n"
        f"{CONFIG['untrusted_input_close']}\n\n"
        f"{DISCLAIMER}\n\n{marker}"
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


def build_inline_comment(head_sha: str, finding: Finding, marker: str) -> ReviewCommentRequest:
    """Build the standalone inline comment request for one anchorable finding."""

    return ReviewCommentRequest(
        commit_id=head_sha,
        path=finding.path,
        line=finding.line,
        side=finding.side,
        body=comment_body(finding, marker),
    )


def build_verdict_review(
    head_sha: str,
    out_of_bounds: list[Finding],
    event: str,
    summary_line: str,
    marker: str,
) -> ReviewPayload:
    """Build the final verdict review: the summary body plus any findings too large to anchor inline."""

    body = summary_line
    if out_of_bounds:
        listed = "\n".join(
            f"- {finding.path}:{finding.line} — {finding_severity_line(finding)} — {finding_category_footer(finding)} — "
            f"{finding.body}"
            for finding in out_of_bounds
        )
        body = f"{body}\n\nFindings not posted inline:\n{listed}"

    body = (
        f"{CONFIG['untrusted_input_open']}\n{body}\n{CONFIG['untrusted_input_close']}\n\n"
        f"{DISCLAIMER}\n\n{marker}"
    )

    return ReviewPayload(commit_id=head_sha, event=event, body=body, comments=[])


async def post_review_or_warn(repo: str, pr_number: int, payload: ReviewPayload, event: str) -> None:
    """Post the review and warn when GitHub rejects it."""

    if not await post_review(repo, pr_number, payload):
        logger.warning("Could not post the %s review; the check run still records the verdict.", event)


async def note_diff_too_large(pr: PullRequestContext, marker: str) -> ReviewRoundResult:
    """Post a note that the diff is too large to auto-review, record the verdict, and return success."""

    body = f"The diff is too large to auto-review, so this review was skipped.\n\n{DISCLAIMER}\n\n{marker}"
    check_id = None if SETTINGS.approval_disable else await start_check_run(pr.repo, pr.head_sha)

    await post_review(pr.repo, pr.number, ReviewPayload(commit_id=pr.head_sha, event="COMMENT", body=body, comments=[]))
    await complete_check_run(pr.repo, check_id, "neutral", "Diff too large", "The diff is too large to auto-review.")

    return ReviewRoundResult(exit_code=0)


async def run_review_round(pr: PullRequestContext, marker: str, get_findings: GetFindings) -> ReviewRoundResult:
    """Stream a backend's findings, posting each anchorable one as it arrives, then record the verdict."""

    # The verdict of record is the check run (approval on) or the review marker (approval off).
    already = (
        await already_reviewed(pr.repo, pr.number, pr.head_sha, marker)
        if SETTINGS.approval_disable
        else await head_check_concluded(pr.repo, pr.head_sha)
    )
    if already:
        logger.info("Head %s already reviewed; skipping.", pr.head_sha)

        return ReviewRoundResult(exit_code=0)

    diff = await pull_request_diff_if_available(pr.repo, pr.number)
    if diff is None:
        logger.warning("PR diff is too large to auto-review; posting a note and skipping.")

        return await note_diff_too_large(pr, marker)

    (anchors, unpatched), posted_findings = await asyncio.gather(
        diff_anchors(pr.repo, pr.number),
        existing_finding_titles(pr.repo, pr.number, marker),
    )
    inputs = ReviewInputs(pr=pr, diff=diff, posted_findings=posted_findings)

    check_id = None if SETTINGS.approval_disable else await start_check_run(pr.repo, pr.head_sha)
    concluded = False
    posted_any = False
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
        # With streaming the comments post mid-run, so the head is gated once here, before the first
        # one. A mid-stream advance posts on the prior head and self-heals on the next run's reconcile.
        if await current_head_sha(pr.repo, pr.number) != pr.head_sha:
            logger.info("Head moved before review; skipping (the new commit reviews next).")
            await complete_check_run(pr.repo, check_id, "cancelled", "Superseded", "The head moved before review.")
            concluded = True

            return ReviewRoundResult(exit_code=0)

        reviewed_files = set(anchors) | unpatched
        threads = await list_review_threads(pr.repo, pr.number)
        posted_keys = extract_posted_keys(threads, marker)

        seen_anchor_keys: set[tuple[str, int, DiffSide, str]] = set()
        seen_new_keys: set[tuple[str, str]] = set()
        current_keys: set[tuple[str, str]] = set()
        severity_by_key: dict[tuple[str, str], Severity] = {}
        out_of_bounds: list[Finding] = []
        low_count = 0
        total_count = 0

        try:
            async for finding in stream_findings_with_retry(get_findings, inputs):
                if not finding_kept(finding):
                    continue

                anchor_key = (finding.path, finding.line, finding.side, finding.title.strip())
                if anchor_key in seen_anchor_keys:
                    continue

                seen_anchor_keys.add(anchor_key)
                title_key = (finding.path, finding.title.strip())
                current_keys.add(title_key)
                severity_by_key[title_key] = finding.severity

                if title_key in posted_keys or title_key in seen_new_keys:
                    continue

                if not is_postable(finding, anchors, unpatched):
                    continue

                # Claim the (path, title) slot for the first postable finding before the cap check, so a
                # later same-titled finding cannot slip past a capped earlier one.
                seen_new_keys.add(title_key)

                if not cap_decision(finding, low_count, total_count):
                    continue

                if finding_anchors(finding, anchors):
                    posted_inline = await post_comment(pr.repo, pr.number, build_inline_comment(pr.head_sha, finding, marker))
                    if not posted_inline:
                        logger.warning("Could not post inline finding %s:%s.", finding.path, finding.line)
                        current_keys.discard(title_key)
                        severity_by_key.pop(title_key, None)

                        continue

                    posted_any = True
                else:
                    out_of_bounds.append(finding)

                total_count += 1
                if finding.severity is Severity.LOW:
                    low_count += 1
        except ReviewBackendError as exc:
            logger.error("Review backend failed: %s", exc)
            await complete_check_run(pr.repo, check_id, "action_required", "Review failed", str(exc))
            concluded = True

            return ReviewRoundResult(exit_code=1, diff=diff)

        open_existing, stale_ids, kept_blocking = classify_threads(threads, marker, current_keys, reviewed_files)

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

        # Post the verdict review only when this round produced something new; otherwise the check run
        # carries the verdict.
        if (posted_any or out_of_bounds or SETTINGS.approval_disable) and not await already_reviewed(
            pr.repo, pr.number, pr.head_sha, marker
        ):
            payload = build_verdict_review(pr.head_sha, out_of_bounds, event, summary, marker)
            await post_review_or_warn(pr.repo, pr.number, payload, event)

        logger.info("Resolving %d stale thread(s); %d open issue(s) remain.", len(stale_ids), open_count)
        await resolve_threads(pr.repo, stale_ids)

        await complete_check_run(pr.repo, check_id, conclusion, title, summary)
        concluded = True

        return ReviewRoundResult(exit_code=0, diff=diff)
    except asyncio.CancelledError:
        await complete_check_run(pr.repo, check_id, "cancelled", "Superseded", "The review job was cancelled.")
        concluded = True

        return ReviewRoundResult(exit_code=1)
    finally:
        for cancel_signal in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(cancel_signal)

        if not concluded:
            await complete_check_run(
                pr.repo, check_id, "action_required", "Review failed", "The review run did not complete."
            )
