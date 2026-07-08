import asyncio
import subprocess
from unittest.mock import AsyncMock

import pytest

from code_review.config import CONFIG
from code_review.models.severity import Severity
from code_review.review_threads import (
    classify_threads,
    existing_finding_titles,
    extract_posted_keys,
    is_tier_comment,
)

MARKER = CONFIG["review_marker"]


class TestTierComment:
    """Test that runner-owned review comments are recognized by marker."""

    @pytest.mark.parametrize(
        "author",
        ["github-actions[bot]", "code-review-app[bot]", "reviewer-pat"],
        ids=["github-actions", "github-app", "pat-user"],
    )
    def test_marked_comment_is_tier_for_any_author(self, thread_comment_factory, author: str) -> None:
        """Test that a comment carrying the marker is recognized as the runner's whatever the author."""

        comment = thread_comment_factory(marker=MARKER, author=author)

        assert is_tier_comment(comment, MARKER) is True

    def test_unmarked_comment_is_not_tier(self, thread_comment_factory) -> None:
        """Test that a comment without the review marker is not recognized as the runner's."""

        comment = thread_comment_factory(body="### Title\n\nA human note.")

        assert is_tier_comment(comment, MARKER) is False


class TestExistingFindingTitles:
    """Test that the runner's posted findings are collected per file."""

    def test_collects_runner_threads(self, monkeypatch, review_thread_factory) -> None:
        """Test that only threads carrying the review marker contribute posted findings."""

        threads = [
            review_thread_factory(title="Mine", severity="Critical", marker=MARKER, path="src/app.py"),
            review_thread_factory(title="Human", path="src/app.py", body="### Human\n\nA human note."),
        ]
        monkeypatch.setattr("code_review.review_threads.list_review_threads", AsyncMock(return_value=threads))

        result = asyncio.run(existing_finding_titles("octo/repo", 7, MARKER))

        assert list(result) == ["src/app.py"]
        assert result["src/app.py"][0].title == "Mine"
        assert result["src/app.py"][0].severity == "critical"

    def test_thread_listing_failure_raises(self, monkeypatch) -> None:
        """Test that thread listing failures are not treated as an empty prior-finding set."""

        monkeypatch.setattr(
            "code_review.review_threads.list_review_threads",
            AsyncMock(side_effect=subprocess.CalledProcessError(1, ["gh", "api"])),
        )

        with pytest.raises(subprocess.CalledProcessError):
            asyncio.run(existing_finding_titles("octo/repo", 7, MARKER))


class TestExtractPostedKeys:
    """Test that the runner's already-posted keys are pulled from the review threads."""

    def test_collects_runner_keys(self, review_thread_factory) -> None:
        """Test that only marker-carrying runner threads contribute posted keys."""

        threads = [
            review_thread_factory(title="Mine", path="src/app.py", marker=MARKER),
            review_thread_factory(title="Human", path="src/app.py", body="### Human\n\nA human note."),
        ]

        assert extract_posted_keys(threads, MARKER) == {("src/app.py", "Mine")}


class TestClassifyThreads:
    """Test that gone findings resolve or stay open per the outdated/blocking rules."""

    def test_classifies(self, mock_config, review_thread_factory) -> None:
        """Test that current threads stay open, non-blocking gone threads go stale, blocking ones stay."""

        mock_config(approval_include=frozenset({Severity.CRITICAL}))
        threads = [
            review_thread_factory(id="current", title="Current", path="src/app.py", marker=MARKER),
            review_thread_factory(
                id="gone-low", title="GoneLow", severity="Medium", path="src/app.py", marker=MARKER
            ),
            review_thread_factory(
                id="gone-critical", title="GoneCrit", severity="Critical", path="src/app.py", marker=MARKER
            ),
        ]

        open_keys, stale_ids, kept_blocking = classify_threads(
            threads, MARKER, {("src/app.py", "Current")}, {"src/app.py"}
        )

        assert ("src/app.py", "Current") in open_keys
        assert stale_ids == ["gone-low"]
        assert ("src/app.py", "GoneCrit") in kept_blocking
        assert ("src/app.py", "GoneCrit") in open_keys
