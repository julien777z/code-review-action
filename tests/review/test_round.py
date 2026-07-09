import asyncio
import subprocess
from unittest.mock import AsyncMock

import pytest

from code_review.config import CONFIG
from code_review.errors import ReviewBackendError
from code_review.models.severity import Severity
from code_review.review.findings import REVIEW_BACKEND_ATTEMPTS
from code_review.review.round import run_review_round

MARKER = CONFIG["review_marker"]


class TestRunReviewRound:
    """Test that the round streams findings, posts each inline, and records the verdict."""

    def test_posts_each_anchorable_finding_inline(
        self, mock_config, review_github_mocks, stream_findings_factory, pull_request_factory, finding_factory
    ) -> None:
        """Test that each anchorable finding is posted as its own inline comment as it streams."""

        review_github_mocks["diff_anchors"].return_value = ({"src/app.py": ({10, 20}, set())}, set())
        findings = [
            finding_factory(path="src/app.py", line=10, title="A"),
            finding_factory(path="src/app.py", line=20, title="B"),
        ]

        result = asyncio.run(run_review_round(pull_request_factory(), MARKER, stream_findings_factory(findings)))

        assert result.exit_code == 0
        assert result.diff == ""
        assert review_github_mocks["post_comment"].await_count == 2

    def test_diff_too_large_posts_note_and_skips(
        self, mock_config, review_github_mocks, stream_findings_factory, pull_request_factory
    ) -> None:
        """Test that an oversized diff posts a note, records a neutral verdict, and returns success."""

        review_github_mocks["pull_request_diff_if_available"].return_value = None

        result = asyncio.run(run_review_round(pull_request_factory(), MARKER, stream_findings_factory([])))

        assert result.exit_code == 0
        review_github_mocks["post_review"].assert_awaited_once()
        body = review_github_mocks["post_review"].await_args.args[2].body

        assert "too large to auto-review" in body
        assert review_github_mocks["complete_check_run"].await_args.args[2] == "neutral"

    def test_non_size_diff_error_propagates(
        self, mock_config, review_github_mocks, stream_findings_factory, pull_request_factory
    ) -> None:
        """Test that a diff fetch failure unrelated to size still fails loudly."""

        review_github_mocks["pull_request_diff_if_available"].side_effect = subprocess.CalledProcessError(
            1, ["gh", "pr", "diff"], stderr="gh: Not Found (HTTP 404)"
        )

        with pytest.raises(subprocess.CalledProcessError):
            asyncio.run(run_review_round(pull_request_factory(), MARKER, stream_findings_factory([])))

    def test_out_of_bounds_goes_to_verdict_body(
        self, mock_config, review_github_mocks, stream_findings_factory, pull_request_factory, finding_factory
    ) -> None:
        """Test that a finding on a too-large unpatched file goes into the verdict review, not an inline comment."""

        review_github_mocks["diff_anchors"].return_value = ({}, {"big.txt"})
        findings = [finding_factory(path="big.txt", line=1, title="Big", severity=Severity.HIGH)]

        asyncio.run(run_review_round(pull_request_factory(), MARKER, stream_findings_factory(findings)))

        assert review_github_mocks["post_comment"].await_count == 0
        assert review_github_mocks["post_review"].await_count == 1

        payload = review_github_mocks["post_review"].await_args.args[2]

        assert payload.comments == []
        assert "Findings not posted inline:" in payload.body

    @pytest.mark.parametrize(
        ("config_overrides", "severities", "expected_posts"),
        [
            ({"low_findings_cap": 1}, [Severity.LOW, Severity.LOW, Severity.HIGH], 2),
            ({"max_findings": 1}, [Severity.HIGH, Severity.HIGH], 1),
        ],
        ids=["low-cap", "total-cap"],
    )
    def test_caps_bound_inline_comments(
        self,
        mock_config,
        review_github_mocks,
        stream_findings_factory,
        pull_request_factory,
        finding_factory,
        config_overrides,
        severities,
        expected_posts,
    ) -> None:
        """Test that the low and total caps bound how many inline comments post."""

        mock_config(**config_overrides)
        lines = list(range(1, len(severities) + 1))
        review_github_mocks["diff_anchors"].return_value = ({"src/app.py": (set(lines), set())}, set())
        findings = [
            finding_factory(path="src/app.py", line=line, title=f"T{line}", severity=severity)
            for line, severity in zip(lines, severities)
        ]

        asyncio.run(run_review_round(pull_request_factory(), MARKER, stream_findings_factory(findings)))

        assert review_github_mocks["post_comment"].await_count == expected_posts

    def test_does_not_repost_already_posted_finding(
        self,
        mock_config,
        review_github_mocks,
        stream_findings_factory,
        pull_request_factory,
        finding_factory,
        review_thread_factory,
    ) -> None:
        """Test that a finding already posted on the PR is not posted again."""

        review_github_mocks["diff_anchors"].return_value = ({"src/app.py": ({10}, set())}, set())
        review_github_mocks["list_review_threads"].return_value = [
            review_thread_factory(title="Off-by-one error", path="src/app.py", marker=MARKER)
        ]
        findings = [finding_factory(path="src/app.py", line=10, title="Off-by-one error")]

        asyncio.run(run_review_round(pull_request_factory(), MARKER, stream_findings_factory(findings)))

        assert review_github_mocks["post_comment"].await_count == 0

    def test_head_advanced_before_review_skips(
        self, mock_config, review_github_mocks, stream_findings_factory, pull_request_factory, finding_factory
    ) -> None:
        """Test that an advanced head before streaming skips the round without posting."""

        review_github_mocks["current_head_sha"].return_value = "moved-sha"
        review_github_mocks["diff_anchors"].return_value = ({"src/app.py": ({10}, set())}, set())
        findings = [finding_factory(path="src/app.py", line=10)]

        result = asyncio.run(
            run_review_round(pull_request_factory(head_sha="abc123"), MARKER, stream_findings_factory(findings))
        )

        assert result.exit_code == 0
        assert review_github_mocks["post_comment"].await_count == 0
        assert review_github_mocks["post_review"].await_count == 0
        assert review_github_mocks["complete_check_run"].await_args.args[2] == "cancelled"

    def test_backend_failure_before_post_concludes_failed(
        self, mock_config, review_github_mocks, flaky_stream_factory, pull_request_factory
    ) -> None:
        """Test that a backend failure before any comment concludes the check as failed and posts nothing."""

        get_findings, _ = flaky_stream_factory(
            failures=REVIEW_BACKEND_ATTEMPTS, error=ReviewBackendError("model error", retryable=False)
        )

        result = asyncio.run(run_review_round(pull_request_factory(), MARKER, get_findings))

        assert result.exit_code == 1
        assert review_github_mocks["post_comment"].await_count == 0
        assert review_github_mocks["post_review"].await_count == 0
        assert review_github_mocks["complete_check_run"].await_args.args[2] == "action_required"

    def test_rejected_inline_post_goes_to_verdict_body(
        self, mock_config, review_github_mocks, stream_findings_factory, pull_request_factory, finding_factory
    ) -> None:
        """Test that a finding whose inline post is rejected is still visible in the verdict body."""

        review_github_mocks["post_comment"].return_value = False
        review_github_mocks["diff_anchors"].return_value = ({"src/app.py": ({10}, set())}, set())
        findings = [finding_factory(path="src/app.py", line=10, title="Off-by-one error", severity=Severity.HIGH)]

        result = asyncio.run(run_review_round(pull_request_factory(), MARKER, stream_findings_factory(findings)))

        assert result.exit_code == 0
        assert review_github_mocks["post_comment"].await_count == 1
        assert review_github_mocks["post_review"].await_count == 1
        payload = review_github_mocks["post_review"].await_args.args[2]

        assert "Findings not posted inline:" in payload.body
        assert "Off-by-one error" not in payload.body
        assert "The loop overruns the array." in payload.body
        assert review_github_mocks["complete_check_run"].await_args.args[2] == "neutral"

    def test_approval_disable_posts_verdict_review_to_record_head(
        self, mock_config, review_github_mocks, stream_findings_factory, pull_request_factory
    ) -> None:
        """Test that approval-disable mode posts the verdict review even with no findings to record the head."""

        mock_config(approval_disable=True)

        asyncio.run(run_review_round(pull_request_factory(), MARKER, stream_findings_factory([])))

        assert review_github_mocks["post_review"].await_count == 1


class TestRunReviewRoundTimeout:
    """Test that a timed-out round records the cut-off without approving or resolving stale threads."""

    def test_timeout_without_findings_concludes_timed_out(
        self,
        mock_config,
        monkeypatch,
        review_github_mocks,
        stream_findings_factory,
        pull_request_factory,
        round_findings_factory,
    ) -> None:
        """Test that a timed-out round with no findings concludes timed_out, so a re-trigger is not skipped."""

        monkeypatch.setattr(
            "code_review.review.round.collect_round_findings",
            AsyncMock(return_value=round_findings_factory(timed_out=True)),
        )

        result = asyncio.run(run_review_round(pull_request_factory(), MARKER, stream_findings_factory([])))

        assert result.exit_code == 0
        assert review_github_mocks["post_review"].await_count == 0
        assert review_github_mocks["complete_check_run"].await_args.args[2] == "timed_out"
        assert review_github_mocks["complete_check_run"].await_args.args[3] == "Review timed out"
        assert "time limit" in review_github_mocks["complete_check_run"].await_args.args[4]
        assert review_github_mocks["resolve_threads"].await_args.args[1] == []

    def test_timeout_with_findings_notes_cutoff_in_verdict(
        self,
        mock_config,
        monkeypatch,
        review_github_mocks,
        stream_findings_factory,
        pull_request_factory,
        round_findings_factory,
    ) -> None:
        """Test that a timed-out round with open non-blocking findings still comments and notes the cut-off."""

        key = ("src/app.py", "Bug")
        monkeypatch.setattr(
            "code_review.review.round.collect_round_findings",
            AsyncMock(
                return_value=round_findings_factory(
                    current_keys={key},
                    severity_by_key={key: Severity.HIGH},
                    posted_any=True,
                    timed_out=True,
                )
            ),
        )

        result = asyncio.run(run_review_round(pull_request_factory(), MARKER, stream_findings_factory([])))

        assert result.exit_code == 0
        assert review_github_mocks["post_review"].await_count == 1
        assert review_github_mocks["complete_check_run"].await_args.args[2] == "timed_out"
        assert "time limit" in review_github_mocks["post_review"].await_args.args[2].body

    def test_timeout_with_blocking_finding_stays_retriggerable(
        self,
        mock_config,
        monkeypatch,
        review_github_mocks,
        stream_findings_factory,
        pull_request_factory,
        round_findings_factory,
    ) -> None:
        """Test that a blocking finding found before the limit still concludes timed_out, not a re-review-blocking verdict."""

        key = ("src/app.py", "Crash")
        monkeypatch.setattr(
            "code_review.review.round.collect_round_findings",
            AsyncMock(
                return_value=round_findings_factory(
                    current_keys={key},
                    severity_by_key={key: Severity.CRITICAL},
                    posted_any=True,
                    timed_out=True,
                )
            ),
        )

        asyncio.run(run_review_round(pull_request_factory(), MARKER, stream_findings_factory([])))

        assert review_github_mocks["complete_check_run"].await_args.args[2] == "timed_out"
        assert review_github_mocks["resolve_threads"].await_args.args[1] == []

    def test_external_cancellation_concludes_superseded(
        self, mock_config, monkeypatch, review_github_mocks, stream_findings_factory, pull_request_factory
    ) -> None:
        """Test that a signal-driven cancellation still concludes the check as superseded, not timed out."""

        monkeypatch.setattr(
            "code_review.review.round.collect_round_findings",
            AsyncMock(side_effect=asyncio.CancelledError()),
        )

        result = asyncio.run(run_review_round(pull_request_factory(), MARKER, stream_findings_factory([])))

        assert result.exit_code == 1
        assert review_github_mocks["complete_check_run"].await_args.args[2] == "cancelled"
        assert review_github_mocks["complete_check_run"].await_args.args[3] == "Superseded"
