import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from code_review.errors import ReviewBackendError
from code_review.models.findings import Finding
from code_review.models.severity import DiffSide, Severity
from code_review.review.findings import (
    REVIEW_BACKEND_ATTEMPTS,
    cap_decision,
    collect_round_findings,
    finding_anchors,
    finding_kept,
    is_postable,
    stream_findings_with_retry,
)


async def collect(stream: AsyncIterator[Finding]) -> list[Finding]:
    """Drain an async finding stream into a list."""

    return [finding async for finding in stream]


class TestStreamFindingsWithRetry:
    """Test that the streaming backend wrapper retries only before the first finding is produced."""

    def test_retries_before_first_finding(self, flaky_stream_factory, review_inputs_factory, finding_factory) -> None:
        """Test that a retryable failure before any finding is retried until findings stream."""

        finding = finding_factory()
        get_findings, calls = flaky_stream_factory(
            failures=1, error=ReviewBackendError("Bridge request timed out", retryable=True), result=[finding]
        )

        result = asyncio.run(collect(stream_findings_with_retry(get_findings, review_inputs_factory())))

        assert result == [finding]
        assert len(calls) == 2

    def test_no_retry_after_a_finding_is_yielded(
        self, flaky_stream_factory, review_inputs_factory, finding_factory
    ) -> None:
        """Test that a retryable failure after a finding has streamed is not retried."""

        get_findings, calls = flaky_stream_factory(
            failures=1,
            error=ReviewBackendError("dropped mid-stream", retryable=True),
            result=[finding_factory()],
            yield_before_error=True,
        )

        with pytest.raises(ReviewBackendError):
            asyncio.run(collect(stream_findings_with_retry(get_findings, review_inputs_factory())))

        assert len(calls) == 1

    def test_raises_after_exhausting_attempts(self, flaky_stream_factory, review_inputs_factory) -> None:
        """Test that a persistently failing backend gives up after the attempt budget."""

        get_findings, calls = flaky_stream_factory(
            failures=REVIEW_BACKEND_ATTEMPTS, error=ReviewBackendError("timed out", retryable=True)
        )

        with pytest.raises(ReviewBackendError):
            asyncio.run(collect(stream_findings_with_retry(get_findings, review_inputs_factory())))

        assert len(calls) == REVIEW_BACKEND_ATTEMPTS

    def test_non_retryable_error_not_retried(self, flaky_stream_factory, review_inputs_factory) -> None:
        """Test that a non-retryable backend error surfaces immediately without further attempts."""

        get_findings, calls = flaky_stream_factory(
            failures=REVIEW_BACKEND_ATTEMPTS, error=ReviewBackendError("unparseable reply", retryable=False)
        )

        with pytest.raises(ReviewBackendError):
            asyncio.run(collect(stream_findings_with_retry(get_findings, review_inputs_factory())))

        assert len(calls) == 1


class TestFindingKept:
    """Test that a finding is kept only when it clears the severity bar and the path filters."""

    @pytest.mark.parametrize(
        ("config_overrides", "finding_overrides", "kept"),
        [
            ({"min_severity": Severity.HIGH}, {"severity": Severity.LOW}, False),
            ({"min_severity": Severity.HIGH}, {"severity": Severity.CRITICAL}, True),
            ({"exclude_paths": ("*.lock",)}, {"path": "poetry.lock"}, False),
            ({"include_paths": ("src/**",)}, {"path": "docs/readme.md"}, False),
            ({"include_paths": ("src/**",)}, {"path": "src/app.py"}, True),
        ],
        ids=["below-min", "meets-min", "excluded", "not-included", "included"],
    )
    def test_finding_kept(self, mock_config, finding_factory, config_overrides, finding_overrides, kept) -> None:
        """Test that severity and path filters decide whether a finding is kept."""

        mock_config(**config_overrides)

        assert finding_kept(finding_factory(**finding_overrides)) is kept


class TestCapDecision:
    """Test that the cap decision honors the running Low and total caps."""

    @pytest.mark.parametrize(
        ("config_overrides", "severity", "low_count", "total_count", "expected"),
        [
            ({"low_findings_cap": 1}, Severity.LOW, 0, 0, True),
            ({"low_findings_cap": 1}, Severity.LOW, 1, 1, False),
            ({"low_findings_cap": 1}, Severity.HIGH, 1, 1, True),
            ({"max_findings": 2}, Severity.HIGH, 0, 2, False),
            ({"max_findings": 2}, Severity.HIGH, 0, 1, True),
        ],
        ids=["low-under-cap", "low-over-cap", "high-ignores-low-cap", "total-cap-hit", "total-cap-under"],
    )
    def test_cap_decision(
        self, mock_config, finding_factory, config_overrides, severity, low_count, total_count, expected
    ) -> None:
        """Test that the running low and total caps decide whether a finding posts."""

        mock_config(**config_overrides)

        assert cap_decision(finding_factory(severity=severity), low_count, total_count) is expected


class TestAnchoring:
    """Test that anchoring and postability follow the diff's available lines."""

    def test_finding_anchors_on_right(self, finding_factory) -> None:
        """Test that a right-side finding anchors when its line is in the new-side set."""

        anchors = {"src/app.py": ({10}, set())}

        assert finding_anchors(finding_factory(line=10, side=DiffSide.RIGHT), anchors) is True

    def test_unanchored_finding_is_postable_when_unpatched(self, finding_factory) -> None:
        """Test that a finding on a too-large unpatched file is still postable."""

        finding = finding_factory(path="big.txt", line=1)

        assert is_postable(finding, {}, {"big.txt"}) is True

    def test_unanchored_finding_not_postable_otherwise(self, finding_factory) -> None:
        """Test that an unanchorable finding on a patched file is not postable."""

        finding = finding_factory(path="src/app.py", line=999)

        assert is_postable(finding, {"src/app.py": ({10}, set())}, set()) is False


class TestCollectRoundFindings:
    """Test that collected findings track only visible review content."""

    def test_capped_finding_is_not_current(
        self,
        mock_config,
        monkeypatch,
        pull_request_factory,
        review_inputs_factory,
        stream_findings_factory,
        finding_factory,
    ) -> None:
        """Test that a capped finding cannot affect thread reconciliation or blocking state."""

        mock_config(max_findings=0)
        monkeypatch.setattr("code_review.review.findings.post_comment", AsyncMock(return_value=True))
        finding = finding_factory(path="src/app.py", line=10, severity=Severity.CRITICAL)

        result = asyncio.run(
            collect_round_findings(
                pull_request_factory(),
                "marker",
                stream_findings_factory([finding]),
                review_inputs_factory(),
                {"src/app.py": ({10}, set())},
                set(),
                set(),
            )
        )

        assert result.current_keys == set()
        assert result.severity_by_key == {}
        assert result.needs_verdict_review is False

    def test_existing_reposted_finding_remains_current(
        self,
        pull_request_factory,
        review_inputs_factory,
        stream_findings_factory,
        finding_factory,
    ) -> None:
        """Test that a finding already posted on the PR keeps its thread current."""

        finding = finding_factory(path="src/app.py", line=10, title="Already posted")

        result = asyncio.run(
            collect_round_findings(
                pull_request_factory(),
                "marker",
                stream_findings_factory([finding]),
                review_inputs_factory(),
                {"src/app.py": ({10}, set())},
                set(),
                {("src/app.py", "Already posted")},
            )
        )

        assert result.current_keys == {("src/app.py", "Already posted")}
        assert result.severity_by_key[("src/app.py", "Already posted")] is Severity.HIGH


class TestCollectRoundFindingsTimeout:
    """Test that the review time limit finalizes with partial findings and cleans up the backend."""

    def test_timeout_keeps_partial_findings_and_cleans_up(
        self,
        mock_config,
        monkeypatch,
        override_review_timeout,
        pull_request_factory,
        review_inputs_factory,
        blocking_stream_factory,
        finding_factory,
    ) -> None:
        """Test that hitting the limit marks the round timed out, keeps streamed findings, and runs cleanup."""

        override_review_timeout(timedelta(seconds=0.1))
        monkeypatch.setattr("code_review.review.findings.post_comment", AsyncMock(return_value=True))
        finding = finding_factory(path="src/app.py", line=10, title="Streamed")
        get_findings, state = blocking_stream_factory([finding])

        result = asyncio.run(
            collect_round_findings(
                pull_request_factory(),
                "marker",
                get_findings,
                review_inputs_factory(),
                {"src/app.py": ({10}, set())},
                set(),
                set(),
            )
        )

        assert result.timed_out is True
        assert result.current_keys == {("src/app.py", "Streamed")}
        assert state["cleaned_up"] is True

    def test_disabled_timeout_drains_stream(
        self,
        mock_config,
        monkeypatch,
        override_review_timeout,
        pull_request_factory,
        review_inputs_factory,
        stream_findings_factory,
        finding_factory,
    ) -> None:
        """Test that a disabled timeout leaves the round un-flagged and drains the whole stream."""

        override_review_timeout(None)
        monkeypatch.setattr("code_review.review.findings.post_comment", AsyncMock(return_value=True))
        finding = finding_factory(path="src/app.py", line=10, title="Streamed")

        result = asyncio.run(
            collect_round_findings(
                pull_request_factory(),
                "marker",
                stream_findings_factory([finding]),
                review_inputs_factory(),
                {"src/app.py": ({10}, set())},
                set(),
                set(),
            )
        )

        assert result.timed_out is False
        assert result.current_keys == {("src/app.py", "Streamed")}
