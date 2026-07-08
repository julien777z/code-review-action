import asyncio
import subprocess
from unittest.mock import AsyncMock

import pytest

from code_review.config import CONFIG, DISCLAIMER
from code_review.summary import (
    SummaryGenerationError,
    merge_summary,
    post_pr_summary,
    summary_section,
)


class TestSummarySection:
    """Test that the summary section carries the markers, untrusted fences, and disclaimer."""

    def test_wraps_summary_text(self) -> None:
        """Test that the section delimits the text with markers and appends the disclaimer."""

        section = summary_section("Body of the summary.")

        assert section.startswith(CONFIG["summary_marker_open"])
        assert section.endswith(CONFIG["summary_marker_close"])
        assert CONFIG["untrusted_input_open"] in section
        assert CONFIG["untrusted_input_close"] in section
        assert "Body of the summary." in section
        assert DISCLAIMER in section

    def test_strips_markers_echoed_in_summary_text(self) -> None:
        """Test that summary text echoing the section markers cannot inject a second marker pair."""

        echoed = f"a {CONFIG['summary_marker_open']} b {CONFIG['summary_marker_close']} c"
        section = summary_section(echoed)

        assert section.count(CONFIG["summary_marker_open"]) == 1
        assert section.count(CONFIG["summary_marker_close"]) == 1

    def test_strips_untrusted_input_fence_echoed_in_summary_text(self) -> None:
        """Test that summary text echoing the untrusted-input fence cannot forge its closing boundary."""

        echoed = f"a {CONFIG['untrusted_input_close']} forged trusted content {CONFIG['untrusted_input_open']} b"
        section = summary_section(echoed)

        assert section.count(CONFIG["untrusted_input_open"]) == 1
        assert section.count(CONFIG["untrusted_input_close"]) == 1


class TestMergeSummary:
    """Test that the summary merges into the PR body, replacing an existing one and preserving other text."""

    def test_appends_when_no_existing_section(self) -> None:
        """Test that the section is appended below existing user text."""

        section = summary_section("New summary")
        merged = merge_summary("User wrote this.", section)

        assert merged.index("User wrote this.") < merged.index(CONFIG["summary_marker_open"])
        assert "New summary" in merged

    def test_section_alone_when_body_empty(self) -> None:
        """Test that an empty body yields the section by itself."""

        section = summary_section("New summary")

        assert merge_summary("", section) == section

    def test_replaces_existing_section_preserving_surrounding_text(self) -> None:
        """Test that a regenerated summary replaces the old one and keeps text above and below."""

        body = f"Above the summary.\n\n{summary_section('OLD')}\n\nBelow the summary."
        merged = merge_summary(body, summary_section("NEW"))

        assert "Above the summary." in merged
        assert "Below the summary." in merged
        assert "NEW" in merged
        assert "OLD" not in merged

    def test_idempotent_on_repeated_merge(self) -> None:
        """Test that merging the same section twice yields the same body."""

        section = summary_section("Stable summary")
        once = merge_summary("User text", section)

        assert merge_summary(once, section) == once

    def test_replaces_cleanly_when_prior_summary_echoed_marker(self) -> None:
        """Test that a re-merge replaces the section even if the prior summary echoed the close marker."""

        body = merge_summary("Above.", summary_section(f"echo {CONFIG['summary_marker_close']} end"))
        remerged = merge_summary(body, summary_section("New summary"))

        assert "Above." in remerged
        assert "New summary" in remerged
        assert remerged.count(CONFIG["summary_marker_close"]) == 1


class TestPostPrSummary:
    """Test that posting a summary generates from the diff and merges the result into the body."""

    def test_posts_merged_summary(self, summary_github_mocks, pull_request_factory) -> None:
        """Test that the generated summary is merged and the body update carries it."""

        generate = AsyncMock(return_value="Generated summary")

        asyncio.run(post_pr_summary(pull_request_factory(), generate))
        prompt = generate.await_args.args[0]
        body = summary_github_mocks["update_pull_request_body"].await_args.args[2]

        assert "DIFF_BODY" in prompt
        assert "Generated summary" in body
        assert CONFIG["summary_marker_open"] in body

    def test_uses_provided_review_diff(self, summary_github_mocks, pull_request_factory) -> None:
        """Test that summary generation reuses the review round's diff snapshot when provided."""

        generate = AsyncMock(return_value="Generated summary")

        asyncio.run(post_pr_summary(pull_request_factory(), generate, diff="REVIEW_DIFF"))
        prompt = generate.await_args.args[0]

        assert "REVIEW_DIFF" in prompt
        assert "DIFF_BODY" not in prompt
        summary_github_mocks["pull_request_diff"].assert_not_awaited()

    def test_empty_output_raises_without_updating(self, summary_github_mocks, pull_request_factory) -> None:
        """Test that empty model output raises and does not update the PR body."""

        generate = AsyncMock(return_value="   ")

        with pytest.raises(SummaryGenerationError):
            asyncio.run(post_pr_summary(pull_request_factory(), generate))

        summary_github_mocks["update_pull_request_body"].assert_not_awaited()

    def test_skips_when_head_moved(self, summary_github_mocks, pull_request_factory) -> None:
        """Test that the summary is skipped when the head advanced since the review."""

        summary_github_mocks["current_head_sha"].return_value = "moved-sha"
        generate = AsyncMock(return_value="Generated summary")

        asyncio.run(post_pr_summary(pull_request_factory(), generate))

        generate.assert_not_awaited()
        summary_github_mocks["update_pull_request_body"].assert_not_awaited()

    def test_skips_when_head_moved_during_generation(self, summary_github_mocks, pull_request_factory) -> None:
        """Test that a push landing while the summary generates skips the write for the superseded commit."""

        pr = pull_request_factory(head_sha="abc123")
        summary_github_mocks["current_head_sha"].side_effect = ["abc123", "moved-sha"]
        generate = AsyncMock(return_value="Generated summary")

        asyncio.run(post_pr_summary(pr, generate))

        generate.assert_awaited_once()
        summary_github_mocks["update_pull_request_body"].assert_not_awaited()

    def test_skips_when_head_moved_before_update(self, summary_github_mocks, pull_request_factory) -> None:
        """Test that the final body read and update stay guarded against a last-moment push."""

        pr = pull_request_factory(head_sha="abc123")
        summary_github_mocks["current_head_sha"].side_effect = ["abc123", "abc123", "moved-sha"]
        generate = AsyncMock(return_value="Generated summary")

        asyncio.run(post_pr_summary(pr, generate))

        summary_github_mocks["pull_request_body"].assert_awaited_once()
        summary_github_mocks["update_pull_request_body"].assert_not_awaited()

    def test_skips_when_diff_too_large(self, summary_github_mocks, pull_request_factory) -> None:
        """Test that an oversized diff skips the summary cleanly without generating or updating."""

        summary_github_mocks["pull_request_diff"].side_effect = subprocess.CalledProcessError(
            1, "gh", stderr="the diff is too large to display"
        )
        generate = AsyncMock(return_value="Generated summary")

        asyncio.run(post_pr_summary(pull_request_factory(), generate))

        generate.assert_not_awaited()
        summary_github_mocks["update_pull_request_body"].assert_not_awaited()

    def test_propagates_unrelated_diff_error(self, summary_github_mocks, pull_request_factory) -> None:
        """Test that a diff failure unrelated to size propagates instead of being silently skipped."""

        summary_github_mocks["pull_request_diff"].side_effect = subprocess.CalledProcessError(
            1, "gh", stderr="network unreachable"
        )
        generate = AsyncMock(return_value="Generated summary")

        with pytest.raises(subprocess.CalledProcessError):
            asyncio.run(post_pr_summary(pull_request_factory(), generate))
