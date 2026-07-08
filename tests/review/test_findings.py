import asyncio
from collections.abc import AsyncIterator

import pytest

from code_review.errors import ReviewBackendError
from code_review.models.findings import Finding
from code_review.models.severity import DiffSide, Severity
from code_review.review.findings import (
    REVIEW_BACKEND_ATTEMPTS,
    cap_decision,
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
