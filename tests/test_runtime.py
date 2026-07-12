import asyncio
from collections.abc import AsyncIterator

import pytest

from code_review.config import CONFIG
from code_review.errors import ReviewBackendError
from code_review.models.backend import Backend, BackendHandlers
from code_review.models.config import ReviewModel
from code_review.models.findings import Finding
from code_review.models.github_event import GithubEvent
from code_review.models.review import FlushCompletion
from code_review.review_backends import claude, codex
from code_review.runtime import (
    BACKENDS,
    backend_findings_session,
    capture_flush_marker,
    generate_summary_with_fallback,
    is_eligible,
    main,
    reaction_subject,
    resolve_pr_number,
    select_backends,
)


async def collect_session_findings(handlers, inputs, *, flush: bool = False) -> list[Finding]:
    """Open a backend findings session and drain its review or flush stream."""

    async with backend_findings_session(handlers, inputs) as session:
        stream = session["flush_findings"]() if flush else session["findings"]()

        return [finding async for finding in stream]


class TestReactionSubject:
    """Test reaction targets for automatic and comment-triggered reviews."""

    def test_comment_trigger_targets_comment(self, issue_comment_event_factory) -> None:
        """Test that a manual trigger reacts on the triggering comment."""

        event = issue_comment_event_factory(comment_id=555)

        assert reaction_subject("issue_comment", event, "octo/repo", 7) == "repos/octo/repo/issues/comments/555"


class TestResolvePrNumber:
    """Test PR-number resolution from supported event types."""

    def test_dispatch_uses_configured_number(self, mock_config) -> None:
        """Test that workflow dispatch reads the explicit action input."""

        mock_config(pr_number=33)

        assert resolve_pr_number("workflow_dispatch", GithubEvent()) == 33


class TestIsEligible:
    """Test the event, fork, bot, and trigger gates."""

    def test_same_repo_pull_request_allowed(self, pull_request_event_factory) -> None:
        """Test that a same-repository PR is eligible."""

        assert is_eligible("pull_request", pull_request_event_factory()) is True

    def test_fork_rejected(self, pull_request_event_factory) -> None:
        """Test that forked PRs cannot receive subscription credentials."""

        assert is_eligible(
            "pull_request", pull_request_event_factory(head_full_name="forker/repo")
        ) is False

    def test_bot_comment_rejected(self, issue_comment_event_factory) -> None:
        """Test that bot-authored trigger comments do not recurse."""

        assert is_eligible(
            "issue_comment", issue_comment_event_factory(sender_type="Bot")
        ) is False


class TestSelectBackends:
    """Test primary provider selection and subscription fallback order."""

    def test_auto_prefers_claude_then_codex(self, mock_config) -> None:
        """Test that auto mode uses Claude first and Codex as its fallback."""

        mock_config(
            review_model=ReviewModel.AUTO,
            claude_code_oauth_token="claude-token",
            codex_auth_json='{"auth_mode":"chatgpt"}',
        )

        assert select_backends() == (Backend.CLAUDE, Backend.CODEX)

    def test_explicit_codex_still_falls_back(self, mock_config) -> None:
        """Test that explicit Codex uses Claude only after subscription exhaustion."""

        mock_config(
            review_model=ReviewModel.CODEX,
            claude_code_oauth_token="claude-token",
            codex_auth_json='{"auth_mode":"chatgpt"}',
        )

        assert select_backends() == (Backend.CODEX, Backend.CLAUDE)

    def test_disabled_fallback_returns_only_primary(self, mock_config) -> None:
        """Test that the fallback input limits selection to one provider."""

        mock_config(
            claude_code_oauth_token="claude-token",
            codex_auth_json='{"auth_mode":"chatgpt"}',
            fallback_on_usage_limit=False,
        )

        assert select_backends() == (Backend.CLAUDE,)

    def test_missing_credentials_skips(self, mock_config) -> None:
        """Test that a run without subscription credentials has no backend."""

        mock_config()

        assert select_backends() == ()


class TestBackends:
    """Test that each provider maps to its local subscription implementation."""

    @pytest.mark.parametrize(
        ("backend", "review_session", "generate_summary"),
        [
            (Backend.CLAUDE, claude.review_session, claude.generate_text),
            (Backend.CODEX, codex.review_session, codex.generate_text),
        ],
        ids=["claude", "codex"],
    )
    def test_handlers(self, backend, review_session, generate_summary) -> None:
        """Test that handler registration uses the expected backend functions."""

        handlers = BACKENDS[backend]

        assert handlers["review_session"] is review_session
        assert handlers["generate_summary"] is generate_summary


class TestBackendReviewPolicy:
    """Test shared JSONL parsing and empty-output policy."""

    def test_parses_backend_text_findings(
        self, review_session_opener_factory, review_inputs_factory
    ) -> None:
        """Test that text from either local agent is parsed into findings."""

        async def review_text() -> AsyncIterator[str]:
            yield '{"path":"a.py","line":1,"side":"RIGHT","severity":"high","title":"A","body":"B"}\n'

        handlers = BackendHandlers(
            review_session=review_session_opener_factory(review_text),
            generate_summary=claude.generate_text,
            label="Claude",
        )

        findings = asyncio.run(collect_session_findings(handlers, review_inputs_factory()))

        assert [finding.title for finding in findings] == ["A"]

    def test_empty_backend_output_raises(
        self, review_session_opener_factory, review_inputs_factory
    ) -> None:
        """Test that empty model output cannot silently approve a PR."""

        async def review_text() -> AsyncIterator[str]:
            return
            yield ""

        handlers = BackendHandlers(
            review_session=review_session_opener_factory(review_text),
            generate_summary=claude.generate_text,
            label="Claude",
        )

        with pytest.raises(ReviewBackendError, match="produced no output"):
            asyncio.run(collect_session_findings(handlers, review_inputs_factory()))


async def capture(*parts: str) -> tuple[list[str], FlushCompletion]:
    """Drain flush-marker capture over the supplied chunks."""

    async def chunks() -> AsyncIterator[str]:
        for part in parts:
            yield part

    completion = FlushCompletion()
    lines = [line async for line in capture_flush_marker(chunks(), completion)]

    return lines, completion


class TestCaptureFlushMarker:
    """Test standalone completion-marker capture."""

    def test_captures_split_marker(self) -> None:
        """Test that a marker split across chunks still records completion."""

        marker = CONFIG["flush_complete_marker"]
        lines, completion = asyncio.run(capture(marker[:6], f"{marker[6:]}\n"))

        assert lines == []
        assert completion.complete is True


class TestSummaryFallback:
    """Test summary provider fallback on subscription exhaustion."""

    def test_switches_only_for_usage_limit(self) -> None:
        """Test that Codex receives the summary after Claude usage is exhausted."""

        async def exhausted(prompt: str) -> str:
            raise ReviewBackendError("limit", usage_limited=True)

        async def succeeds(prompt: str) -> str:
            return "summary"

        handlers = (
            BackendHandlers(review_session=claude.review_session, generate_summary=exhausted, label="Claude"),
            BackendHandlers(review_session=codex.review_session, generate_summary=succeeds, label="Codex"),
        )

        assert asyncio.run(generate_summary_with_fallback(handlers, "prompt")) == "summary"


class TestMain:
    """Test top-level review and summary orchestration."""

    def test_first_event_posts_summary(self, main_harness) -> None:
        """Test that an opened PR still receives its configured summary."""

        mocks = main_harness(action="opened")

        assert asyncio.run(main()) == 0
        mocks["run_backend_review"].assert_awaited_once_with(mocks["pr"], (mocks["handlers"],))
        mocks["post_pr_summary"].assert_awaited_once()

    def test_later_push_skips_summary(self, main_harness) -> None:
        """Test that synchronize events continue to skip PR-description summaries."""

        mocks = main_harness(action="synchronize")

        assert asyncio.run(main()) == 0
        mocks["post_pr_summary"].assert_not_awaited()

    def test_forked_comment_target_skips_before_starting_a_provider(self, main_harness) -> None:
        """Test that manual triggers for forked PRs cannot consume subscription credentials."""

        mocks = main_harness()
        mocks["pr"].head_repo_owner = "forker"

        assert asyncio.run(main()) == 0
        mocks["run_backend_review"].assert_not_awaited()
        mocks["post_pr_summary"].assert_not_awaited()
