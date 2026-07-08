from code_review.config import SETTINGS
from code_review.github import list_review_threads
from code_review.models.pull_request import PostedFinding
from code_review.models.severity import Severity
from code_review.models.threads import ReviewThread, ThreadCommentNode
from code_review.review.comments import thread_severity, thread_title


def is_tier_comment(comment: ThreadCommentNode | None, marker: str) -> bool:
    """Return whether the comment is the runner's own posting."""

    if comment is None:
        return False

    return marker in comment.body


async def existing_finding_titles(repo: str, pr_number: int, marker: str) -> dict[str, list[PostedFinding]]:
    """Return the runner's posted severity/title pairs per file."""

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
    """Return every path/title the runner has already posted."""

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
    """Split the runner's threads into open, stale, and kept blocking keys."""

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

        severity = thread_severity(comment)
        is_blocking = severity is not None and severity in SETTINGS.approval_include

        if thread.is_outdated or (comment.path in reviewed_files and not is_blocking):
            stale_ids.append(thread.id)
        else:
            open_keys.add(key)
            if is_blocking:
                kept_blocking_keys.add(key)

    return open_keys, stale_ids, kept_blocking_keys
