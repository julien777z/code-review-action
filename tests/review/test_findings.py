import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from code_review.errors import ReviewBackendError
from code_review.models.backend import FindingsBackend, FindingsSession
from code_review.models.findings import Finding, FindingCategory
from code_review.models.pull_request import PostedFinding, ReviewInputs
from code_review.models.review import FlushCompletion
from code_review.models.severity import DiffSide, Severity
from code_review.review.findings import (
    collect_round_findings,
    finding_anchors,
    finding_kept,
    flush_budget,
    flush_reserve,
    is_postable,
    low_finding_rank,
    total_cap_reached,
)

ANCHORS_UP_TO_LINE_30 = {"src/app.py": ({10, 20, 30}, set())}


def posted_titles(post_comment: AsyncMock) -> list[str]:
    """Return the finding titles from posted inline comments, in call order."""

    return [
        row[4:].strip()
        for call in post_comment.call_args_list
        for row in call.args[2].body.splitlines()
        if row.startswith("### ")
    ]


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
        post_comment_mock,
        pull_request_factory,
        review_inputs_factory,
        findings_session_factory,
        finding_factory,
    ) -> None:
        """Test that a capped finding cannot affect thread reconciliation or blocking state."""

        mock_config(max_findings=0)
        finding = finding_factory(path="src/app.py", line=10, severity=Severity.CRITICAL)

        result = asyncio.run(
            collect_round_findings(
                pull_request_factory(),
                "marker",
                findings_session_factory([finding])[0],
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
        findings_session_factory,
        finding_factory,
    ) -> None:
        """Test that a finding already posted on the PR keeps its thread current."""

        finding = finding_factory(path="src/app.py", line=10, title="Already posted")

        result = asyncio.run(
            collect_round_findings(
                pull_request_factory(),
                "marker",
                findings_session_factory([finding])[0],
                review_inputs_factory(),
                {"src/app.py": ({10}, set())},
                set(),
                {("src/app.py", "Already posted")},
            )
        )

        assert result.current_keys == {("src/app.py", "Already posted")}
        assert result.severity_by_key[("src/app.py", "Already posted")] is Severity.HIGH

    def test_usage_fallback_refreshes_comments_and_adds_only_missing_context(
        self,
        monkeypatch,
        post_comment_mock,
        pull_request_factory,
        review_inputs_factory,
        finding_factory,
    ) -> None:
        """Test that a replacement provider receives refreshed comments plus only unseen findings."""

        visible = finding_factory(line=10, title="Visible")
        not_visible = finding_factory(line=20, title="Not visible")
        captured: list[ReviewInputs] = []

        @asynccontextmanager
        async def first(inputs: ReviewInputs) -> AsyncIterator[FindingsSession]:
            async def _findings() -> AsyncIterator[Finding]:
                yield visible
                yield not_visible
                raise ReviewBackendError("limit", usage_limited=True)

            async def _empty() -> AsyncIterator[Finding]:
                return
                yield visible

            yield FindingsSession(
                findings=_findings,
                flush_findings=_empty,
                flush_completion=FlushCompletion(),
            )

        @asynccontextmanager
        async def second(inputs: ReviewInputs) -> AsyncIterator[FindingsSession]:
            captured.append(inputs)

            async def _empty() -> AsyncIterator[Finding]:
                return
                yield visible

            yield FindingsSession(
                findings=_empty,
                flush_findings=_empty,
                flush_completion=FlushCompletion(),
            )

        monkeypatch.setattr(
            "code_review.review.findings.existing_finding_titles",
            AsyncMock(return_value={"src/app.py": [PostedFinding(severity="high", title="Visible")]}),
        )
        backends = (
            FindingsBackend(label="Claude", open_session=first),
            FindingsBackend(label="Codex", open_session=second),
        )

        asyncio.run(
            collect_round_findings(
                pull_request_factory(),
                "marker",
                backends,
                review_inputs_factory(),
                ANCHORS_UP_TO_LINE_30,
                set(),
                set(),
            )
        )

        assert len(captured) == 1
        assert captured[0].provider_handoff is not None
        assert "Claude reached" in captured[0].provider_handoff
        assert "Codex provider" in captured[0].provider_handoff
        assert [finding.title for finding in captured[0].posted_findings["src/app.py"]] == [
            "Visible",
            "Not visible",
        ]

    def test_usage_fallback_covers_session_startup(
        self, monkeypatch, pull_request_factory, review_inputs_factory
    ) -> None:
        """Test that a usage limit raised before streaming still starts the replacement provider."""

        async def empty_findings() -> AsyncIterator[Finding]:
            return
            yield

        @asynccontextmanager
        async def exhausted(inputs: ReviewInputs) -> AsyncIterator[FindingsSession]:
            raise ReviewBackendError("limit", usage_limited=True)
            yield FindingsSession(
                findings=empty_findings,
                flush_findings=empty_findings,
                flush_completion=FlushCompletion(),
            )

        @asynccontextmanager
        async def replacement(inputs: ReviewInputs) -> AsyncIterator[FindingsSession]:
            yield FindingsSession(
                findings=empty_findings,
                flush_findings=empty_findings,
                flush_completion=FlushCompletion(),
            )

        backends = (
            FindingsBackend(label="Claude", open_session=exhausted),
            FindingsBackend(label="Codex", open_session=replacement),
        )
        monkeypatch.setattr(
            "code_review.review.findings.existing_finding_titles", AsyncMock(return_value={})
        )

        result = asyncio.run(
            collect_round_findings(
                pull_request_factory(),
                "marker",
                backends,
                review_inputs_factory(),
                ANCHORS_UP_TO_LINE_30,
                set(),
                set(),
            )
        )

        assert result.current_keys == set()


class TestDeferredLows:
    """Test that low findings are buffered during the stream and flushed under the caps at the end."""

    def test_lows_post_after_non_lows(
        self, mock_config, post_comment_mock, pull_request_factory, review_inputs_factory, findings_session_factory, finding_factory
    ) -> None:
        """Test that a low arriving before a medium still posts after it, proving the buffer defers lows."""

        low = finding_factory(line=10, severity=Severity.LOW, title="Low first")
        medium = finding_factory(line=20, severity=Severity.MEDIUM, title="Medium second")

        result = asyncio.run(
            collect_round_findings(
                pull_request_factory(),
                "marker",
                findings_session_factory([low, medium])[0],
                review_inputs_factory(),
                ANCHORS_UP_TO_LINE_30,
                set(),
                set(),
            )
        )

        assert posted_titles(post_comment_mock) == ["Medium second", "Low first"]
        assert result.current_keys == {("src/app.py", "Low first"), ("src/app.py", "Medium second")}

    def test_important_low_beats_earlier_low_under_cap(
        self, mock_config, post_comment_mock, pull_request_factory, review_inputs_factory, findings_session_factory, finding_factory
    ) -> None:
        """Test that a later high-priority-category low outranks an earlier one when the low cap is one."""

        mock_config(low_findings_cap=1)
        early = finding_factory(line=10, severity=Severity.LOW, category=FindingCategory.CODE_SIMPLIFICATION, title="Simplify")
        late = finding_factory(line=20, severity=Severity.LOW, category=FindingCategory.BUG, title="Bug low")

        result = asyncio.run(
            collect_round_findings(
                pull_request_factory(),
                "marker",
                findings_session_factory([early, late])[0],
                review_inputs_factory(),
                ANCHORS_UP_TO_LINE_30,
                set(),
                set(),
            )
        )

        assert posted_titles(post_comment_mock) == ["Bug low"]
        assert result.current_keys == {("src/app.py", "Bug low")}

    def test_lows_share_the_total_cap_budget(
        self, mock_config, post_comment_mock, pull_request_factory, review_inputs_factory, findings_session_factory, finding_factory
    ) -> None:
        """Test that lows are dropped when non-lows already fill the total-findings cap."""

        mock_config(max_findings=2)
        findings = [
            finding_factory(line=10, severity=Severity.MEDIUM, title="M1"),
            finding_factory(line=20, severity=Severity.MEDIUM, title="M2"),
            finding_factory(line=30, severity=Severity.LOW, title="L1"),
        ]

        result = asyncio.run(
            collect_round_findings(
                pull_request_factory(),
                "marker",
                findings_session_factory(findings)[0],
                review_inputs_factory(),
                ANCHORS_UP_TO_LINE_30,
                set(),
                set(),
            )
        )

        assert posted_titles(post_comment_mock) == ["M1", "M2"]
        assert result.current_keys == {("src/app.py", "M1"), ("src/app.py", "M2")}

    def test_low_matching_existing_thread_is_tracked_not_buffered(
        self, mock_config, post_comment_mock, pull_request_factory, review_inputs_factory, findings_session_factory, finding_factory
    ) -> None:
        """Test that a low matching an existing thread is tracked current immediately and never posts a new comment."""

        low = finding_factory(line=10, severity=Severity.LOW, title="Existing")

        result = asyncio.run(
            collect_round_findings(
                pull_request_factory(),
                "marker",
                findings_session_factory([low])[0],
                review_inputs_factory(),
                ANCHORS_UP_TO_LINE_30,
                set(),
                {("src/app.py", "Existing")},
            )
        )

        post_comment_mock.assert_not_awaited()
        assert result.current_keys == {("src/app.py", "Existing")}
        assert result.severity_by_key[("src/app.py", "Existing")] is Severity.LOW

    def test_later_non_low_supersedes_earlier_same_title_low(
        self, mock_config, post_comment_mock, pull_request_factory, review_inputs_factory, findings_session_factory, finding_factory
    ) -> None:
        """Test that a non-low emitted after a same-title buffered low still publishes and wins the title."""

        low = finding_factory(line=10, severity=Severity.LOW, category=FindingCategory.CODE_SIMPLIFICATION, title="Shared")
        medium = finding_factory(line=20, severity=Severity.MEDIUM, title="Shared")

        result = asyncio.run(
            collect_round_findings(
                pull_request_factory(),
                "marker",
                findings_session_factory([low, medium])[0],
                review_inputs_factory(),
                ANCHORS_UP_TO_LINE_30,
                set(),
                set(),
            )
        )

        assert posted_titles(post_comment_mock) == ["Shared"]
        assert result.severity_by_key[("src/app.py", "Shared")] is Severity.MEDIUM

    def test_dropped_finding_does_not_claim_its_title(
        self, mock_config, post_comment_mock, pull_request_factory, review_inputs_factory, findings_session_factory, finding_factory
    ) -> None:
        """Test that a finding dropped by the total cap does not block a later distinct finding sharing its title."""

        mock_config(max_findings=1)
        first = finding_factory(line=10, severity=Severity.MEDIUM, title="Kept")
        dropped = finding_factory(line=20, severity=Severity.MEDIUM, title="Contested")

        result = asyncio.run(
            collect_round_findings(
                pull_request_factory(),
                "marker",
                findings_session_factory([first, dropped])[0],
                review_inputs_factory(),
                ANCHORS_UP_TO_LINE_30,
                set(),
                set(),
            )
        )

        assert posted_titles(post_comment_mock) == ["Kept"]
        assert ("src/app.py", "Contested") not in result.current_keys

    def test_buffered_lows_dedupe_by_title(
        self, mock_config, post_comment_mock, pull_request_factory, review_inputs_factory, findings_session_factory, finding_factory
    ) -> None:
        """Test that two buffered lows sharing a title on different lines post only once."""

        first = finding_factory(line=10, severity=Severity.LOW, title="Duplicate")
        second = finding_factory(line=20, severity=Severity.LOW, title="Duplicate")

        result = asyncio.run(
            collect_round_findings(
                pull_request_factory(),
                "marker",
                findings_session_factory([first, second])[0],
                review_inputs_factory(),
                ANCHORS_UP_TO_LINE_30,
                set(),
                set(),
            )
        )

        assert posted_titles(post_comment_mock) == ["Duplicate"]
        assert result.current_keys == {("src/app.py", "Duplicate")}


class TestFlushReserve:
    """Test that the flush reserve caps at three minutes and scales down with short budgets."""

    @pytest.mark.parametrize(
        ("review_timeout", "expected"),
        [
            (timedelta(minutes=15), timedelta(minutes=3)),
            (timedelta(minutes=5), timedelta(minutes=1)),
            (timedelta(minutes=30), timedelta(minutes=3)),
        ],
        ids=["default", "short", "long"],
    )
    def test_flush_reserve(self, review_timeout: timedelta, expected: timedelta) -> None:
        """Test that the reserve is a fifth of the budget capped at three minutes."""

        assert flush_reserve(review_timeout) == expected

    @pytest.mark.parametrize(
        ("review_timeout", "expected"),
        [
            (timedelta(minutes=15), timedelta(seconds=160)),
            (timedelta(minutes=1), timedelta(seconds=8)),
        ],
        ids=["default", "one-minute"],
    )
    def test_flush_budget(self, review_timeout: timedelta, expected: timedelta) -> None:
        """Test that the flush window subtracts a headroom that shrinks with the reserve."""

        assert flush_budget(review_timeout) == expected

    def test_flush_budget_is_positive_for_every_configured_timeout(self) -> None:
        """Test that any whole-minute timeout funds a real flush window."""

        assert all(flush_budget(timedelta(minutes=minutes)) > timedelta(0) for minutes in range(1, 61))


class TestCollectRoundFindingsTimeout:
    """Test that the soft deadline interrupts the review, flushes the session, and keeps partial findings."""

    def test_timeout_keeps_partial_findings_and_cleans_up(
        self,
        mock_config,
        post_comment_mock,
        override_review_timeout,
        pull_request_factory,
        review_inputs_factory,
        findings_session_factory,
        finding_factory,
    ) -> None:
        """Test that hitting the limit marks the round timed out, keeps streamed findings, and closes the session."""

        override_review_timeout(timedelta(seconds=0.1))
        finding = finding_factory(path="src/app.py", line=10, title="Streamed")
        open_session, state = findings_session_factory([finding], block_after_review=True)

        result = asyncio.run(
            collect_round_findings(
                pull_request_factory(),
                "marker",
                open_session,
                review_inputs_factory(),
                {"src/app.py": ({10}, set())},
                set(),
                set(),
            )
        )

        assert result.timed_out is True
        assert result.current_keys == {("src/app.py", "Streamed")}
        assert state.closed == 1

    def test_short_timeout_still_runs_the_flush_turn(
        self,
        mock_config,
        post_comment_mock,
        override_review_timeout,
        pull_request_factory,
        review_inputs_factory,
        findings_session_factory,
        finding_factory,
    ) -> None:
        """Test that even a short budget funds a flush window that solicits unemitted findings."""

        override_review_timeout(timedelta(seconds=1))
        open_session, state = findings_session_factory(
            [finding_factory()], block_after_review=True, flush_findings=[finding_factory(line=20, title="Late")]
        )

        result = asyncio.run(
            collect_round_findings(
                pull_request_factory(),
                "marker",
                open_session,
                review_inputs_factory(),
                ANCHORS_UP_TO_LINE_30,
                set(),
                set(),
            )
        )

        assert result.timed_out is True
        assert state.flush_calls == 1
        assert ("src/app.py", "Late") in result.current_keys

    def test_flush_turn_posts_remaining_findings(
        self,
        mock_config,
        post_comment_mock,
        override_review_timeout,
        zero_flush_headroom,
        pull_request_factory,
        review_inputs_factory,
        findings_session_factory,
        finding_factory,
    ) -> None:
        """Test that the wrap-up flush publishes the agent's remaining findings after the interruption."""

        mock_config(low_findings_cap=1)
        override_review_timeout(timedelta(seconds=0.5))
        streamed = finding_factory(line=10, severity=Severity.MEDIUM, title="Streamed")
        duplicate = finding_factory(line=10, severity=Severity.MEDIUM, title="Streamed")
        late = finding_factory(line=20, severity=Severity.MEDIUM, title="Late")
        low = finding_factory(line=30, severity=Severity.LOW, category=FindingCategory.BUG, title="Low")
        open_session, state = findings_session_factory(
            [streamed], block_after_review=True, flush_findings=[duplicate, late, low]
        )

        result = asyncio.run(
            collect_round_findings(
                pull_request_factory(),
                "marker",
                open_session,
                review_inputs_factory(),
                ANCHORS_UP_TO_LINE_30,
                set(),
                set(),
            )
        )

        assert result.timed_out is True
        assert state.flush_calls == 1
        assert state.closed == 1
        assert posted_titles(post_comment_mock) == ["Streamed", "Late", "Low"]
        assert result.current_keys == {
            ("src/app.py", "Streamed"),
            ("src/app.py", "Late"),
            ("src/app.py", "Low"),
        }

    def test_complete_flush_clears_the_timed_out_flag(
        self,
        mock_config,
        post_comment_mock,
        override_review_timeout,
        zero_flush_headroom,
        pull_request_factory,
        review_inputs_factory,
        findings_session_factory,
        finding_factory,
    ) -> None:
        """Test that a flush asserting full coverage concludes the round as a normal review."""

        override_review_timeout(timedelta(seconds=0.5))
        open_session, state = findings_session_factory(
            [finding_factory()], block_after_review=True, flush_findings=[], flush_complete=True
        )

        result = asyncio.run(
            collect_round_findings(
                pull_request_factory(),
                "marker",
                open_session,
                review_inputs_factory(),
                ANCHORS_UP_TO_LINE_30,
                set(),
                set(),
            )
        )

        assert result.timed_out is False
        assert state.flush_calls == 1

    def test_usage_limited_flush_switches_to_the_other_provider(
        self,
        mock_config,
        monkeypatch,
        post_comment_mock,
        override_review_timeout,
        zero_flush_headroom,
        pull_request_factory,
        review_inputs_factory,
        findings_session_factory,
        finding_factory,
    ) -> None:
        """Test that a flush usage limit starts the replacement provider with the same round context."""

        override_review_timeout(timedelta(seconds=0.5))
        monkeypatch.setattr("code_review.review.findings.existing_finding_titles", AsyncMock(return_value={}))
        primary, primary_state = findings_session_factory(
            [finding_factory(line=10, title="Streamed")],
            block_after_review=True,
            flush_error=ReviewBackendError("limit", usage_limited=True),
        )
        fallback, fallback_state = findings_session_factory([finding_factory(line=20, title="Recovered")])

        result = asyncio.run(
            collect_round_findings(
                pull_request_factory(),
                "marker",
                primary + fallback,
                review_inputs_factory(),
                ANCHORS_UP_TO_LINE_30,
                set(),
                set(),
            )
        )

        assert result.timed_out is True
        assert primary_state.flush_calls == 1
        assert fallback_state.opened == 1
        assert posted_titles(post_comment_mock) == ["Streamed", "Recovered"]

    @pytest.mark.parametrize(
        ("session_overrides", "expected_flush_calls"),
        [
            ({"flush_error": ReviewBackendError("flush dropped", retryable=True)}, 1),
            ({"block_in_flush": True}, 1),
            ({"flush_findings": [], "flush_complete": False}, 1),
        ],
        ids=["flush-error-not-retried", "flush-hard-timeout", "partial-flush"],
    )
    def test_failed_or_partial_flush_keeps_the_round_timed_out(
        self,
        mock_config,
        post_comment_mock,
        override_review_timeout,
        zero_flush_headroom,
        pull_request_factory,
        review_inputs_factory,
        findings_session_factory,
        finding_factory,
        session_overrides,
        expected_flush_calls,
    ) -> None:
        """Test that a failing, hanging, or partial flush keeps the phase-one findings and the timed-out verdict."""

        override_review_timeout(timedelta(seconds=0.5))
        finding = finding_factory(path="src/app.py", line=10, title="Streamed")
        open_session, state = findings_session_factory([finding], block_after_review=True, **session_overrides)

        result = asyncio.run(
            collect_round_findings(
                pull_request_factory(),
                "marker",
                open_session,
                review_inputs_factory(),
                {"src/app.py": ({10}, set())},
                set(),
                set(),
            )
        )

        assert result.timed_out is True
        assert result.current_keys == {("src/app.py", "Streamed")}
        assert state.flush_calls == expected_flush_calls

    def test_retry_before_first_finding_reopens_a_fresh_session(
        self,
        mock_config,
        post_comment_mock,
        pull_request_factory,
        review_inputs_factory,
        findings_session_factory,
        finding_factory,
    ) -> None:
        """Test that a retryable failure before any finding opens a fresh session and closes the failed one."""

        finding = finding_factory(path="src/app.py", line=10, title="Recovered")
        open_session, state = findings_session_factory(
            [finding], review_error=ReviewBackendError("bridge dropped", retryable=True), review_failures=1
        )

        result = asyncio.run(
            collect_round_findings(
                pull_request_factory(),
                "marker",
                open_session,
                review_inputs_factory(),
                {"src/app.py": ({10}, set())},
                set(),
                set(),
            )
        )

        assert result.current_keys == {("src/app.py", "Recovered")}
        assert state.opened == 2
        assert state.closed == 2

    def test_disabled_timeout_drains_stream(
        self,
        mock_config,
        post_comment_mock,
        override_review_timeout,
        pull_request_factory,
        review_inputs_factory,
        findings_session_factory,
        finding_factory,
    ) -> None:
        """Test that a disabled timeout leaves the round un-flagged and drains the whole stream."""

        override_review_timeout(None)
        finding = finding_factory(path="src/app.py", line=10, title="Streamed")

        result = asyncio.run(
            collect_round_findings(
                pull_request_factory(),
                "marker",
                findings_session_factory([finding])[0],
                review_inputs_factory(),
                {"src/app.py": ({10}, set())},
                set(),
                set(),
            )
        )

        assert result.timed_out is False
        assert result.current_keys == {("src/app.py", "Streamed")}
