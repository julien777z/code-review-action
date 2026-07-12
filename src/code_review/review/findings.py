import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from datetime import timedelta
from fnmatch import fnmatch
from typing import Final, TypedDict

from code_review.config import SETTINGS
from code_review.errors import ReviewBackendError
from code_review.github import post_comment
from code_review.models.backend import FindingsBackend, FindingsSession
from code_review.models.findings import Finding, FindingCategory
from code_review.models.pull_request import PostedFinding, PullRequestContext, ReviewInputs
from code_review.models.review import FindingPublication, ReviewPhaseStats, RoundFindings
from code_review.models.severity import DiffSide, Severity
from code_review.review.comments import build_inline_comment
from code_review.review.threads import existing_finding_titles

logger = logging.getLogger("code_review.review.findings")

REVIEW_BACKEND_ATTEMPTS: Final[int] = 3
REVIEW_RETRY_BACKOFF: Final[timedelta] = timedelta(seconds=2)


class DeadlineTiming(TypedDict):
    """Tuning values for the shared review deadline."""

    hurry_max: timedelta
    hurry_fraction: float
    finalization_max: timedelta
    finalization_fraction: int
    cancellation_grace: timedelta


DEADLINE_TIMING: Final[DeadlineTiming] = DeadlineTiming(
    hurry_max=timedelta(minutes=2),
    hurry_fraction=7.5,
    finalization_max=timedelta(seconds=30),
    finalization_fraction=30,
    cancellation_grace=timedelta(seconds=5),
)

LOW_CATEGORY_PRIORITY: Final[dict[FindingCategory, int]] = {
    FindingCategory.SECURITY: 0,
    FindingCategory.BUG: 1,
    FindingCategory.PERFORMANCE: 2,
    FindingCategory.PROJECT_RULE: 3,
    FindingCategory.TESTING: 4,
    FindingCategory.DOCUMENTATION: 5,
    FindingCategory.CODE_SIMPLIFICATION: 6,
    FindingCategory.OTHER: 7,
}


class RoundPublishState(TypedDict):
    """Shared accumulators and diff context threaded through one round's publish phases."""

    anchors: dict[str, tuple[set[int], set[int]]]
    unpatched: set[str]
    posted_keys: set[tuple[str, str]]
    findings: RoundFindings
    deferred_lows: list[Finding]
    seen_anchor_keys: set[tuple[str, int, DiffSide, str]]
    observed_findings: list[Finding]


def hurry_reserve(review_timeout: timedelta) -> timedelta:
    """Return the portion of a review reserved for the hurry-up turn and final output."""

    return min(DEADLINE_TIMING["hurry_max"], review_timeout / DEADLINE_TIMING["hurry_fraction"])


def finalization_reserve(review_timeout: timedelta) -> timedelta:
    """Return the time held back for GitHub publication and process cleanup."""

    return min(
        DEADLINE_TIMING["finalization_max"], review_timeout / DEADLINE_TIMING["finalization_fraction"]
    )


def flush_budget(review_timeout: timedelta) -> timedelta:
    """Return the hurry-up turn's hard window, positive for any configured timeout."""

    return hurry_reserve(review_timeout) - finalization_reserve(review_timeout)


def seconds_remaining(deadline: float) -> float:
    """Return non-negative seconds remaining before an event-loop deadline."""

    return max(0.0, deadline - asyncio.get_running_loop().time())


async def counted_findings(stream: AsyncIterator[Finding], stats: ReviewPhaseStats) -> AsyncIterator[Finding]:
    """Yield findings while counting arrivals and logging when the first one lands."""

    started = asyncio.get_running_loop().time()

    async for finding in stream:
        stats.received += 1
        if stats.received == 1:
            elapsed = asyncio.get_running_loop().time() - started
            logger.info("First %s finding arrived after %.0fs.", stats.label, elapsed)

        yield finding


def path_allowed(path: str) -> bool:
    """Return whether a path passes the include/exclude glob filters."""

    if SETTINGS.include_paths and not any(fnmatch(path, glob) for glob in SETTINGS.include_paths):
        return False

    return not any(fnmatch(path, glob) for glob in SETTINGS.exclude_paths)


def finding_kept(finding: Finding) -> bool:
    """Return whether a finding passes the severity bar and path filters."""

    return finding.severity.meets(SETTINGS.min_severity) and path_allowed(finding.path)


def total_cap_reached(published_count: int) -> bool:
    """Return whether the total-findings cap is already met."""

    return SETTINGS.max_findings is not None and published_count >= SETTINGS.max_findings


def low_finding_rank(finding: Finding, arrival_index: int) -> tuple[int, int]:
    """Rank a buffered low by category importance, then by arrival order."""

    return LOW_CATEGORY_PRIORITY.get(finding.category, len(LOW_CATEGORY_PRIORITY)), arrival_index


def finding_anchors(finding: Finding, anchors: dict[str, tuple[set[int], set[int]]]) -> bool:
    """Return whether the finding's line is present on its diff side."""

    right, left = anchors.get(finding.path, (set(), set()))

    return finding.line in (left if finding.side is DiffSide.LEFT else right)


def is_postable(
    finding: Finding, anchors: dict[str, tuple[set[int], set[int]]], unpatched: set[str]
) -> bool:
    """Return whether a finding can be made visible."""

    return finding_anchors(finding, anchors) or finding.path in unpatched


def finding_title_key(finding: Finding) -> tuple[str, str]:
    """Return the path/title identity used to reconcile review threads."""

    return finding.path, finding.title.strip()


def finding_anchor_key(finding: Finding) -> tuple[str, int, DiffSide, str]:
    """Return the anchor identity used to deduplicate streamed findings."""

    return finding.path, finding.line, finding.side, finding.title.strip()


async def publish_finding(
    pr: PullRequestContext,
    marker: str,
    finding: Finding,
    anchors: dict[str, tuple[set[int], set[int]]],
) -> FindingPublication:
    """Publish a finding inline when possible, or mark it for the verdict body."""

    if not finding_anchors(finding, anchors):
        return FindingPublication.VERDICT

    posted_inline = await post_comment(pr.repo, pr.number, build_inline_comment(pr.head_sha, finding, marker))
    if posted_inline:
        return FindingPublication.INLINE

    logger.warning("Could not post inline finding %s:%s.", finding.path, finding.line)

    return FindingPublication.VERDICT


async def publish_and_track(pr: PullRequestContext, marker: str, finding: Finding, state: RoundPublishState) -> None:
    """Publish a finding and record it as current and published in the round accumulator."""

    publication = await publish_finding(pr, marker, finding, state["anchors"])
    state["findings"].track_current(finding_title_key(finding), finding)
    state["findings"].track_publication(finding, publication)


async def publish_round_findings(
    pr: PullRequestContext, marker: str, findings_stream: AsyncIterator[Finding], state: RoundPublishState
) -> None:
    """Stream and deduplicate findings, publishing non-lows immediately and buffering lows for the round's end."""

    findings = state["findings"]

    async for finding in findings_stream:
        if not finding_kept(finding):
            continue

        anchor_key = finding_anchor_key(finding)
        if anchor_key in state["seen_anchor_keys"]:
            continue

        state["seen_anchor_keys"].add(anchor_key)
        title_key = finding_title_key(finding)

        if not is_postable(finding, state["anchors"], state["unpatched"]):
            continue

        state["observed_findings"].append(finding)

        if title_key in state["posted_keys"]:
            findings.track_current(title_key, finding)

            continue

        if title_key in findings.current_keys:
            continue

        if finding.severity is Severity.LOW:
            state["deferred_lows"].append(finding)

            continue

        if total_cap_reached(findings.published_count):
            continue

        await publish_and_track(pr, marker, finding, state)


async def publish_deferred_lows(pr: PullRequestContext, marker: str, state: RoundPublishState) -> None:
    """Publish the most important buffered low findings within the low and total caps."""

    findings = state["findings"]
    low_slots = SETTINGS.low_findings_cap
    if SETTINGS.max_findings is not None:
        low_slots = min(low_slots, max(0, SETTINGS.max_findings - findings.published_count))

    ranked = sorted(enumerate(state["deferred_lows"]), key=lambda item: low_finding_rank(item[1], item[0]))

    posted = 0
    for _, finding in ranked:
        if posted >= low_slots:
            break

        if finding_title_key(finding) in findings.current_keys:
            continue

        await publish_and_track(pr, marker, finding, state)
        posted += 1


async def flush_round_findings(
    pr: PullRequestContext, marker: str, session: FindingsSession, budget: timedelta, state: RoundPublishState
) -> bool:
    """Run the wrap-up flush turn under the remaining hard budget, returning whether the agent reported completion."""

    flush_stats = ReviewPhaseStats(label="flush")
    try:
        async with asyncio.timeout(budget.total_seconds()):
            await publish_round_findings(
                pr, marker, counted_findings(session["flush_findings"](), flush_stats), state
            )
    except TimeoutError:
        logger.warning("The wrap-up flush hit its hard deadline after %d finding(s).", flush_stats.received)
    except ReviewBackendError as exc:
        if exc.usage_limited:
            raise

        logger.warning("The wrap-up flush failed; keeping the review-phase findings: %s", exc)
    else:
        logger.info("The wrap-up flush produced %d finding(s).", flush_stats.received)

        return session["flush_completion"].complete

    return False


async def collect_round_findings(
    pr: PullRequestContext,
    marker: str,
    backends: tuple[FindingsBackend, ...],
    inputs: ReviewInputs,
    anchors: dict[str, tuple[set[int], set[int]]],
    unpatched: set[str],
    posted_keys: set[tuple[str, str]],
    *,
    hurry_at: float | None = None,
    model_deadline: float | None = None,
) -> RoundFindings:
    """Run review streaming until its shared deadline, then ask the live agent to finish immediately."""

    review_timeout = SETTINGS.review_timeout
    now = asyncio.get_running_loop().time()
    if review_timeout is not None and hurry_at is None:
        hurry_at = now + (review_timeout - hurry_reserve(review_timeout)).total_seconds()
    if review_timeout is not None and model_deadline is None:
        model_deadline = now + (review_timeout - finalization_reserve(review_timeout)).total_seconds()
    state = RoundPublishState(
        anchors=anchors,
        unpatched=unpatched,
        posted_keys=posted_keys,
        findings=RoundFindings(),
        deferred_lows=[],
        seen_anchor_keys=set(),
        observed_findings=[],
    )

    async with AsyncExitStack() as stack:
        live_session: FindingsSession | None = None
        live_backend_index: int | None = None

        async def fallback_inputs(
            current_inputs: ReviewInputs, previous: str, replacement: str
        ) -> ReviewInputs:
            """Refresh posted findings and add only missing in-memory findings for a provider handoff."""

            posted = await existing_finding_titles(
                current_inputs.pr.repo, current_inputs.pr.number, marker
            )
            posted_keys = {
                (path, finding.title)
                for path, findings in posted.items()
                for finding in findings
            }
            for finding in state["observed_findings"]:
                key = (finding.path, finding.title)
                if key in posted_keys:
                    continue

                posted.setdefault(finding.path, []).append(
                    PostedFinding(severity=finding.severity.value, title=finding.title)
                )
                posted_keys.add(key)

            return current_inputs.model_copy(
                update={
                    "posted_findings": posted,
                    "provider_handoff": (
                        f"{previous} reached its subscription usage limit during this review, so you are "
                        f"continuing the same round as the {replacement} provider. The prior-findings list "
                        "below was refreshed from PR comments and supplemented only with findings that had "
                        "not become visible there. Re-evaluate the full diff and re-emit every still-valid "
                        "finding using the exact existing title and severity."
                    ),
                }
            )

        async def review_findings() -> AsyncIterator[Finding]:
            """Stream providers in fallback order, retrying transient startup failures per provider."""

            nonlocal live_backend_index, live_session
            current_inputs = inputs
            last_error: ReviewBackendError | None = None

            for backend_index, backend in enumerate(backends):
                for attempt in range(REVIEW_BACKEND_ATTEMPTS):
                    produced = False
                    await stack.aclose()
                    live_session = None
                    live_backend_index = backend_index
                    try:
                        live_session = await stack.enter_async_context(
                            backend["open_session"](current_inputs)
                        )
                        logger.info("%s review session is ready.", backend["label"])
                        async for finding in live_session["findings"]():
                            produced = True
                            yield finding

                        return
                    except ReviewBackendError as exc:
                        last_error = exc
                        has_fallback = backend_index < len(backends) - 1
                        if exc.usage_limited and has_fallback:
                            replacement = backends[backend_index + 1]
                            logger.warning(
                                "%s usage is exhausted; continuing the round with %s.",
                                backend["label"],
                                replacement["label"],
                            )
                            current_inputs = await fallback_inputs(
                                current_inputs, backend["label"], replacement["label"]
                            )
                            break

                        if produced or not exc.retryable or attempt == REVIEW_BACKEND_ATTEMPTS - 1:
                            raise

                        backoff = REVIEW_RETRY_BACKOFF * (2**attempt)
                        logger.warning(
                            "%s failed; retrying in %ss: %s",
                            backend["label"],
                            backoff.total_seconds(),
                            exc,
                        )
                        await asyncio.sleep(backoff.total_seconds())
                else:
                    continue

            if last_error is not None:
                raise last_error

        review_stats = ReviewPhaseStats(label="review")
        if hurry_at is not None:
            logger.info("Streaming review findings until the shared hurry deadline.")
        review_task = asyncio.create_task(
            publish_round_findings(pr, marker, counted_findings(review_findings(), review_stats), state)
        )
        try:
            if hurry_at is None:
                await review_task
            else:
                done, _ = await asyncio.wait({review_task}, timeout=seconds_remaining(hurry_at))
                if done:
                    await review_task
                else:
                    state["findings"].timed_out = True
                    logger.warning(
                        "Review reached its hurry deadline with %d finding(s) streamed; asking the agent to finish.",
                        review_stats.received,
                    )
                    review_task.cancel()
                    cancellation_wait = DEADLINE_TIMING["cancellation_grace"].total_seconds()
                    if model_deadline is not None:
                        cancellation_wait = min(cancellation_wait, seconds_remaining(model_deadline))
                    done, _ = await asyncio.wait({review_task}, timeout=cancellation_wait)
                    if not done:
                        logger.warning("The review stream did not stop within %.0fs; concluding without a flush.", cancellation_wait)
                    elif live_session is not None and model_deadline is not None:
                        try:
                            review_task.result()
                        except asyncio.CancelledError:
                            pass

                        budget = timedelta(seconds=seconds_remaining(model_deadline))
                        if budget > timedelta(0):
                            logger.info("Sending the hurry-up turn with %.0fs remaining for model output.", budget.total_seconds())
                            try:
                                completed = await flush_round_findings(pr, marker, live_session, budget, state)
                            except ReviewBackendError as exc:
                                replacement_index = (live_backend_index or 0) + 1
                                if not exc.usage_limited or replacement_index >= len(backends):
                                    raise

                                previous = backends[replacement_index - 1]
                                replacement = backends[replacement_index]
                                logger.warning(
                                    "%s usage is exhausted during the hurry-up turn; continuing with %s.",
                                    previous["label"],
                                    replacement["label"],
                                )
                                handoff_inputs = await fallback_inputs(
                                    inputs, previous["label"], replacement["label"]
                                )
                                await stack.aclose()
                                live_session = await stack.enter_async_context(
                                    replacement["open_session"](handoff_inputs)
                                )
                                handoff_stats = ReviewPhaseStats(label="handoff")
                                try:
                                    async with asyncio.timeout(budget.total_seconds()):
                                        await publish_round_findings(
                                            pr,
                                            marker,
                                            counted_findings(live_session["findings"](), handoff_stats),
                                            state,
                                        )
                                except TimeoutError:
                                    logger.warning(
                                        "The replacement review hit its hard deadline after %d finding(s).",
                                        handoff_stats.received,
                                    )
                                else:
                                    logger.info(
                                        "The replacement review produced %d finding(s).", handoff_stats.received
                                    )
                                completed = False
                            if completed:
                                logger.info("The agent reported the review as complete; concluding with a normal verdict.")
                                state["findings"].timed_out = False
                    else:
                        logger.warning("No live agent session was ready for the hurry-up turn.")
        finally:
            if not review_task.done():
                review_task.cancel()

    await publish_deferred_lows(pr, marker, state)

    return state["findings"]
