import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from code_review.errors import ReviewBackendError
from code_review.models.findings import Finding, FindingCategory
from code_review.models.severity import DiffSide, Severity
from code_review.review.findings import (
    REVIEW_BACKEND_ATTEMPTS,
    collect_round_findings,
    finding_anchors,
    finding_kept,
    is_postable,
    low_finding_rank,
    stream_findings_with_retry,
    total_cap_reached,
)

ANCHORS_UP_TO_LINE_30 = {"src/app.py": ({10, 20, 30}, set())}


async def collect(stream: AsyncIterator[Finding]) -> list[Finding]:
    """Drain an async finding stream into a list."""

    return [finding async for finding in stream]


def posted_titles(post_comment: AsyncMock) -> list[str]:
    """Return the finding titles from posted inline comments, in call order."""

    return [
        row[4:].strip()
        for call in post_comment.call_args_list
        for row in call.args[2].body.splitlines()
        if row.startswith("### ")
    ]


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


class TestTotalCapReached:
    """Test that the total-findings cap gate follows the running published count."""

    @pytest.mark.parametrize(
        ("config_overrides", "published_count", "expected"),
        [
            ({"max_findings": None}, 5, False),
            ({"max_findings": 2}, 1, False),
            ({"max_findings": 2}, 2, True),
            ({"max_findings": 2}, 3, True),
        ],
        ids=["unbounded", "under-cap", "at-cap", "over-cap"],
    )
    def test_total_cap_reached(self, mock_config, config_overrides, published_count, expected) -> None:
        """Test that the total cap is met only once the published count reaches max_findings."""

        mock_config(**config_overrides)

        assert total_cap_reached(published_count) is expected


class TestLowFindingRank:
    """Test that buffered lows rank by category importance, then arrival order."""

    @pytest.mark.parametrize(
        ("higher", "lower"),
        [
            (FindingCategory.SECURITY, FindingCategory.BUG),
            (FindingCategory.BUG, FindingCategory.PERFORMANCE),
            (FindingCategory.PERFORMANCE, FindingCategory.PROJECT_RULE),
            (FindingCategory.PROJECT_RULE, FindingCategory.TESTING),
            (FindingCategory.TESTING, FindingCategory.DOCUMENTATION),
            (FindingCategory.DOCUMENTATION, FindingCategory.CODE_SIMPLIFICATION),
            (FindingCategory.CODE_SIMPLIFICATION, FindingCategory.OTHER),
        ],
        ids=["security>bug", "bug>perf", "perf>rule", "rule>test", "test>docs", "docs>simplify", "simplify>other"],
    )
    def test_more_important_category_ranks_first(self, finding_factory, higher, lower) -> None:
        """Test that a more important category sorts ahead of a less important one at the same arrival."""

        assert low_finding_rank(finding_factory(category=higher), 0) < low_finding_rank(
            finding_factory(category=lower), 0
        )

    def test_ties_break_by_arrival_order(self, finding_factory) -> None:
        """Test that two same-category lows keep their arrival order."""

        finding = finding_factory(category=FindingCategory.BUG)

        assert low_finding_rank(finding, 0) < low_finding_rank(finding, 1)


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


class TestDeferredLows:
    """Test that low findings are buffered during the stream and flushed under the caps at the end."""

    def test_lows_post_after_non_lows(
        self, mock_config, monkeypatch, pull_request_factory, review_inputs_factory, stream_findings_factory, finding_factory
    ) -> None:
        """Test that a low arriving before a medium still posts after it, proving the buffer defers lows."""

        post_comment = AsyncMock(return_value=True)
        monkeypatch.setattr("code_review.review.findings.post_comment", post_comment)
        low = finding_factory(line=10, severity=Severity.LOW, title="Low first")
        medium = finding_factory(line=20, severity=Severity.MEDIUM, title="Medium second")

        result = asyncio.run(
            collect_round_findings(
                pull_request_factory(),
                "marker",
                stream_findings_factory([low, medium]),
                review_inputs_factory(),
                ANCHORS_UP_TO_LINE_30,
                set(),
                set(),
            )
        )

        assert posted_titles(post_comment) == ["Medium second", "Low first"]
        assert result.current_keys == {("src/app.py", "Low first"), ("src/app.py", "Medium second")}

    def test_important_low_beats_earlier_low_under_cap(
        self, mock_config, monkeypatch, pull_request_factory, review_inputs_factory, stream_findings_factory, finding_factory
    ) -> None:
        """Test that a later high-priority-category low outranks an earlier one when the low cap is one."""

        mock_config(low_findings_cap=1)
        post_comment = AsyncMock(return_value=True)
        monkeypatch.setattr("code_review.review.findings.post_comment", post_comment)
        early = finding_factory(line=10, severity=Severity.LOW, category=FindingCategory.CODE_SIMPLIFICATION, title="Simplify")
        late = finding_factory(line=20, severity=Severity.LOW, category=FindingCategory.BUG, title="Bug low")

        result = asyncio.run(
            collect_round_findings(
                pull_request_factory(),
                "marker",
                stream_findings_factory([early, late]),
                review_inputs_factory(),
                ANCHORS_UP_TO_LINE_30,
                set(),
                set(),
            )
        )

        assert posted_titles(post_comment) == ["Bug low"]
        assert result.current_keys == {("src/app.py", "Bug low")}

    def test_lows_share_the_total_cap_budget(
        self, mock_config, monkeypatch, pull_request_factory, review_inputs_factory, stream_findings_factory, finding_factory
    ) -> None:
        """Test that lows are dropped when non-lows already fill the total-findings cap."""

        mock_config(max_findings=2)
        post_comment = AsyncMock(return_value=True)
        monkeypatch.setattr("code_review.review.findings.post_comment", post_comment)
        findings = [
            finding_factory(line=10, severity=Severity.MEDIUM, title="M1"),
            finding_factory(line=20, severity=Severity.MEDIUM, title="M2"),
            finding_factory(line=30, severity=Severity.LOW, title="L1"),
        ]

        result = asyncio.run(
            collect_round_findings(
                pull_request_factory(),
                "marker",
                stream_findings_factory(findings),
                review_inputs_factory(),
                ANCHORS_UP_TO_LINE_30,
                set(),
                set(),
            )
        )

        assert posted_titles(post_comment) == ["M1", "M2"]
        assert result.current_keys == {("src/app.py", "M1"), ("src/app.py", "M2")}

    def test_low_matching_existing_thread_is_tracked_not_buffered(
        self, mock_config, monkeypatch, pull_request_factory, review_inputs_factory, stream_findings_factory, finding_factory
    ) -> None:
        """Test that a low matching an existing thread is tracked current immediately and never posts a new comment."""

        post_comment = AsyncMock(return_value=True)
        monkeypatch.setattr("code_review.review.findings.post_comment", post_comment)
        low = finding_factory(line=10, severity=Severity.LOW, title="Existing")

        result = asyncio.run(
            collect_round_findings(
                pull_request_factory(),
                "marker",
                stream_findings_factory([low]),
                review_inputs_factory(),
                ANCHORS_UP_TO_LINE_30,
                set(),
                {("src/app.py", "Existing")},
            )
        )

        post_comment.assert_not_awaited()
        assert result.current_keys == {("src/app.py", "Existing")}
        assert result.severity_by_key[("src/app.py", "Existing")] is Severity.LOW

    def test_buffered_lows_dedupe_by_title(
        self, mock_config, monkeypatch, pull_request_factory, review_inputs_factory, stream_findings_factory, finding_factory
    ) -> None:
        """Test that two buffered lows sharing a title on different lines post only once."""

        post_comment = AsyncMock(return_value=True)
        monkeypatch.setattr("code_review.review.findings.post_comment", post_comment)
        first = finding_factory(line=10, severity=Severity.LOW, title="Duplicate")
        second = finding_factory(line=20, severity=Severity.LOW, title="Duplicate")

        result = asyncio.run(
            collect_round_findings(
                pull_request_factory(),
                "marker",
                stream_findings_factory([first, second]),
                review_inputs_factory(),
                ANCHORS_UP_TO_LINE_30,
                set(),
                set(),
            )
        )

        assert posted_titles(post_comment) == ["Duplicate"]
        assert result.current_keys == {("src/app.py", "Duplicate")}


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

    def test_timeout_flushes_top_low_after_streamed_non_low(
        self,
        mock_config,
        monkeypatch,
        override_review_timeout,
        pull_request_factory,
        review_inputs_factory,
        blocking_stream_factory,
        finding_factory,
    ) -> None:
        """Test that hitting the limit still posts the streamed non-low and the best buffered low."""

        mock_config(low_findings_cap=1)
        override_review_timeout(timedelta(seconds=0.1))
        post_comment = AsyncMock(return_value=True)
        monkeypatch.setattr("code_review.review.findings.post_comment", post_comment)
        medium = finding_factory(line=10, severity=Severity.MEDIUM, title="Medium")
        low_bug = finding_factory(line=20, severity=Severity.LOW, category=FindingCategory.BUG, title="Bug low")
        low_simplify = finding_factory(
            line=30, severity=Severity.LOW, category=FindingCategory.CODE_SIMPLIFICATION, title="Simplify low"
        )
        get_findings, state = blocking_stream_factory([medium, low_bug, low_simplify])

        result = asyncio.run(
            collect_round_findings(
                pull_request_factory(),
                "marker",
                get_findings,
                review_inputs_factory(),
                ANCHORS_UP_TO_LINE_30,
                set(),
                set(),
            )
        )

        assert result.timed_out is True
        assert state["cleaned_up"] is True
        assert posted_titles(post_comment) == ["Medium", "Bug low"]
        assert result.current_keys == {("src/app.py", "Medium"), ("src/app.py", "Bug low")}

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
