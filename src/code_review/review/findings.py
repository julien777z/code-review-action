import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from fnmatch import fnmatch
from typing import Final

from code_review.config import SETTINGS
from code_review.errors import ReviewBackendError
from code_review.github import post_comment
from code_review.models.backend import GetBackendFindings
from code_review.models.findings import Finding
from code_review.models.pull_request import PullRequestContext, ReviewInputs
from code_review.models.review import FindingPublication, RoundFindings
from code_review.models.severity import DiffSide, Severity
from code_review.review.comments import build_inline_comment

logger = logging.getLogger("code_review.review.findings")

REVIEW_BACKEND_ATTEMPTS: Final[int] = 3
REVIEW_RETRY_BACKOFF: Final[timedelta] = timedelta(seconds=2)


async def stream_findings_with_retry(
    get_findings: GetBackendFindings, inputs: ReviewInputs
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


def path_allowed(path: str) -> bool:
    """Return whether a path passes the include/exclude glob filters."""

    if SETTINGS.include_paths and not any(fnmatch(path, glob) for glob in SETTINGS.include_paths):
        return False

    return not any(fnmatch(path, glob) for glob in SETTINGS.exclude_paths)


def finding_kept(finding: Finding) -> bool:
    """Return whether a finding passes the severity bar and path filters."""

    return finding.severity.meets(SETTINGS.min_severity) and path_allowed(finding.path)


def cap_decision(finding: Finding, low_count: int, total_count: int) -> bool:
    """Return whether to post a finding under the running caps."""

    if SETTINGS.max_findings is not None and total_count >= SETTINGS.max_findings:
        return False

    if finding.severity is Severity.LOW and low_count >= SETTINGS.low_findings_cap:
        return False

    return True


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


async def publish_round_findings(
    pr: PullRequestContext,
    marker: str,
    get_findings: GetBackendFindings,
    inputs: ReviewInputs,
    anchors: dict[str, tuple[set[int], set[int]]],
    unpatched: set[str],
    posted_keys: set[tuple[str, str]],
    findings: RoundFindings,
) -> None:
    """Stream, deduplicate, cap, and publish findings into the round accumulator."""

    seen_anchor_keys: set[tuple[str, int, DiffSide, str]] = set()
    seen_new_keys: set[tuple[str, str]] = set()
    low_count = 0
    total_count = 0

    async for finding in stream_findings_with_retry(get_findings, inputs):
        if not finding_kept(finding):
            continue

        anchor_key = finding_anchor_key(finding)
        if anchor_key in seen_anchor_keys:
            continue

        seen_anchor_keys.add(anchor_key)
        title_key = finding_title_key(finding)

        if not is_postable(finding, anchors, unpatched):
            continue

        if title_key in posted_keys:
            findings.track_current(title_key, finding)

            continue

        if title_key in seen_new_keys:
            continue

        if not cap_decision(finding, low_count, total_count):
            continue

        publication = await publish_finding(pr, marker, finding, anchors)
        findings.track_current(title_key, finding)
        findings.track_publication(finding, publication)
        seen_new_keys.add(title_key)

        total_count += 1
        if finding.severity is Severity.LOW:
            low_count += 1


async def collect_round_findings(
    pr: PullRequestContext,
    marker: str,
    get_findings: GetBackendFindings,
    inputs: ReviewInputs,
    anchors: dict[str, tuple[set[int], set[int]]],
    unpatched: set[str],
    posted_keys: set[tuple[str, str]],
) -> RoundFindings:
    """Stream and publish findings for this review round, bounded by the review deadline."""

    review_timeout = SETTINGS.review_timeout
    deadline_seconds = review_timeout.total_seconds() if review_timeout is not None else None
    findings = RoundFindings()

    try:
        async with asyncio.timeout(deadline_seconds) as review_deadline:
            await publish_round_findings(pr, marker, get_findings, inputs, anchors, unpatched, posted_keys, findings)
    except TimeoutError:
        if not review_deadline.expired():
            raise

        logger.warning("Review hit the %s time limit; finalizing with the findings collected so far.", review_timeout)
        findings.timed_out = True

    return findings
