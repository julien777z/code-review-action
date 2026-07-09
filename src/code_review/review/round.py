import asyncio
import logging
import signal

from code_review.config import DISCLAIMER, SETTINGS
from code_review.errors import ReviewBackendError
from code_review.github import (
    already_reviewed,
    complete_check_run,
    current_head_sha,
    diff_anchors,
    head_check_concluded,
    list_review_threads,
    post_review,
    pull_request_diff_if_available,
    resolve_threads,
    start_check_run,
)
from code_review.models.backend import GetBackendFindings
from code_review.models.findings import ReviewPayload
from code_review.models.pull_request import PullRequestContext, ReviewInputs
from code_review.models.review import ReviewRoundResult
from code_review.review.comments import build_verdict_review, compute_verdict, verdict_summary
from code_review.review.findings import collect_round_findings
from code_review.review.threads import classify_threads, existing_finding_titles, extract_posted_keys

logger = logging.getLogger("code_review.review")


async def post_review_or_warn(repo: str, pr_number: int, payload: ReviewPayload, event: str) -> None:
    """Post the review and warn when GitHub rejects it."""

    if not await post_review(repo, pr_number, payload):
        logger.warning("Could not post the %s review; the check run still records the verdict.", event)


def resolve_round_verdict(
    open_count: int, open_blocking: bool, previous_count: int, timed_out: bool
) -> tuple[str, str, str, str]:
    """Return the review event, check conclusion, title, and summary for this round."""

    if SETTINGS.approval_disable:
        event, conclusion, title = "COMMENT", "neutral", ""
    elif timed_out and not open_blocking:
        event, conclusion, title = "COMMENT", "neutral", "Review timed out"
    else:
        event, conclusion, title = compute_verdict(open_count, open_blocking)

    summary = verdict_summary(event, open_count, previous_count)
    if timed_out:
        summary = (
            f"{summary}\n\nThe review reached its {SETTINGS.review_timeout_minutes}-minute time limit and may be "
            "incomplete; the findings shown reflect only what was reviewed before the limit."
        )

    return event, conclusion, title, summary


async def note_diff_too_large(pr: PullRequestContext, marker: str) -> ReviewRoundResult:
    """Post a note that the diff is too large to auto-review."""

    body = f"The diff is too large to auto-review, so this review was skipped.\n\n{DISCLAIMER}\n\n{marker}"
    check_id = None if SETTINGS.approval_disable else await start_check_run(pr.repo, pr.head_sha)

    await post_review(pr.repo, pr.number, ReviewPayload(commit_id=pr.head_sha, event="COMMENT", body=body, comments=[]))
    await complete_check_run(pr.repo, check_id, "neutral", "Diff too large", "The diff is too large to auto-review.")

    return ReviewRoundResult(exit_code=0)


async def run_review_round(
    pr: PullRequestContext, marker: str, get_findings: GetBackendFindings
) -> ReviewRoundResult:
    """Stream a backend's findings, post review comments, and record the verdict."""

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
    loop = asyncio.get_running_loop()
    review_task = asyncio.current_task()

    def _cancel_on_signal() -> None:
        if review_task is not None:
            review_task.cancel()

    for cancel_signal in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(cancel_signal, _cancel_on_signal)

    try:
        if await current_head_sha(pr.repo, pr.number) != pr.head_sha:
            logger.info("Head moved before review; skipping (the new commit reviews next).")
            await complete_check_run(pr.repo, check_id, "cancelled", "Superseded", "The head moved before review.")
            concluded = True

            return ReviewRoundResult(exit_code=0)

        reviewed_files = set(anchors) | unpatched
        threads = await list_review_threads(pr.repo, pr.number)
        posted_keys = extract_posted_keys(threads, marker)

        try:
            findings = await collect_round_findings(
                pr, marker, get_findings, inputs, anchors, unpatched, posted_keys
            )
        except ReviewBackendError as exc:
            logger.error("Review backend failed: %s", exc)
            await complete_check_run(pr.repo, check_id, "action_required", "Review failed", str(exc))
            concluded = True

            return ReviewRoundResult(exit_code=1, diff=diff)

        open_existing, stale_ids, kept_blocking = classify_threads(
            threads, marker, findings.current_keys, reviewed_files
        )

        new_open_keys = {key for key in findings.current_keys if key not in posted_keys}
        open_keys = open_existing | new_open_keys
        open_count = len(open_keys)
        open_blocking = bool(kept_blocking) or any(
            findings.severity_by_key.get(key) in SETTINGS.approval_include for key in open_keys
        )

        previous_count = len(open_existing)
        event, conclusion, title, summary = resolve_round_verdict(
            open_count, open_blocking, previous_count, findings.timed_out
        )

        if (findings.needs_verdict_review or SETTINGS.approval_disable) and not await already_reviewed(
            pr.repo, pr.number, pr.head_sha, marker
        ):
            payload = build_verdict_review(pr.head_sha, findings.out_of_bounds, event, summary, marker)
            await post_review_or_warn(pr.repo, pr.number, payload, event)

        stale_to_resolve = [] if findings.timed_out else stale_ids
        logger.info("Resolving %d stale thread(s); %d open issue(s) remain.", len(stale_to_resolve), open_count)
        await resolve_threads(pr.repo, stale_to_resolve)

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
