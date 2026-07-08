import asyncio
from collections.abc import AsyncIterator

import anthropic
import httpx
import pytest
from cursor_sdk import CursorAgentError

from code_review.config import ReviewModel
from code_review.models.findings import Finding
from code_review.models.github_event import GithubEvent
from code_review.review_backends import claude, cursor
from code_review.review import ReviewBackendError
from code_review.summary import SummaryGenerationError
from code_review.runtime import (
    BACKENDS,
    Backend,
    BackendHandlers,
    association_allowed,
    is_eligible,
    is_first_review_event,
    main,
    reaction_subject,
    resolve_pr_number,
    select_backend,
    stream_backend_findings,
)


async def collect_findings(findings: AsyncIterator[Finding]) -> list[Finding]:
    """Drain an async finding stream into a list."""

    return [finding async for finding in findings]


class TestAssociationAllowed:
    """Test that the author-association allowlist gates triggering."""

    def test_empty_allows_all(self, mock_config) -> None:
        """Test that an empty allowlist permits any association."""

        mock_config(author_associations=())

        assert association_allowed("NONE") is True

    @pytest.mark.parametrize(("association", "expected"), [("MEMBER", True), ("NONE", False)], ids=["member", "outsider"])
    def test_enforces_allowlist(self, mock_config, association: str, expected: bool) -> None:
        """Test that a non-empty allowlist permits only listed associations."""

        mock_config(author_associations=("MEMBER", "OWNER"))

        assert association_allowed(association) is expected


class TestIsFirstReviewEvent:
    """Test that only opened/ready pull_request events count as the first review."""

    @pytest.mark.parametrize(
        ("action", "expected"),
        [("opened", True), ("ready_for_review", True), ("synchronize", False)],
        ids=["opened", "ready", "sync"],
    )
    def test_pull_request_actions(self, pull_request_event_factory, action: str, expected: bool) -> None:
        """Test that the first-review actions are recognized and others are not."""

        assert is_first_review_event("pull_request", pull_request_event_factory(action=action)) is expected

    def test_non_pull_request(self, issue_comment_event_factory) -> None:
        """Test that a comment event is never the first review."""

        assert is_first_review_event("issue_comment", issue_comment_event_factory()) is False


class TestReactionSubject:
    """Test that the reviewing reaction targets the trigger comment or the pull request."""

    def test_comment_trigger_targets_the_comment(self, issue_comment_event_factory) -> None:
        """Test that a trigger-phrase comment is the reaction subject."""

        event = issue_comment_event_factory(comment_id=555)

        assert reaction_subject("issue_comment", event, "octo/repo", 7) == "repos/octo/repo/issues/comments/555"

    @pytest.mark.parametrize("event_name", ["pull_request", "workflow_dispatch"], ids=["pull_request", "dispatch"])
    def test_other_events_target_the_pull_request(self, pull_request_event_factory, event_name: str) -> None:
        """Test that pull_request and manual-dispatch events react on the PR itself."""

        assert reaction_subject(event_name, pull_request_event_factory(), "octo/repo", 7) == "repos/octo/repo/issues/7"


class TestResolvePrNumber:
    """Test that the PR number resolves from each event type."""

    def test_from_pull_request(self, pull_request_event_factory) -> None:
        """Test that a pull_request event yields its PR number."""

        assert resolve_pr_number("pull_request", pull_request_event_factory(number=12)) == 12

    def test_from_issue_comment(self, issue_comment_event_factory) -> None:
        """Test that a comment event yields the issue number."""

        assert resolve_pr_number("issue_comment", issue_comment_event_factory(number=9)) == 9

    def test_from_dispatch(self, mock_config) -> None:
        """Test that a manual dispatch yields the configured PR number."""

        mock_config(pr_number=33)

        assert resolve_pr_number("workflow_dispatch", GithubEvent()) == 33


class TestIsEligible:
    """Test that fork, bot-comment, association, and trigger gates decide eligibility."""

    def test_pull_request_member(self, mock_config, pull_request_event_factory) -> None:
        """Test that a member's same-repo PR is eligible."""

        mock_config()

        assert is_eligible("pull_request", pull_request_event_factory()) is True

    def test_fork_rejected(self, mock_config, pull_request_event_factory) -> None:
        """Test that a PR from a fork is rejected."""

        mock_config()

        assert is_eligible("pull_request", pull_request_event_factory(head_full_name="forker/repo")) is False

    def test_bot_sender_pull_request_allowed(self, mock_config, pull_request_event_factory) -> None:
        """Test that bot-pushed updates to eligible PRs are allowed."""

        mock_config()

        assert is_eligible("pull_request", pull_request_event_factory(action="synchronize", sender_type="Bot")) is True

    def test_unhandled_action_rejected(self, mock_config, pull_request_event_factory) -> None:
        """Test that a non-review pull_request action is rejected."""

        mock_config()

        assert is_eligible("pull_request", pull_request_event_factory(action="closed")) is False

    def test_association_allowlist(self, mock_config, pull_request_event_factory) -> None:
        """Test that an outsider is rejected when an allowlist is set."""

        mock_config(author_associations=("OWNER",))

        assert is_eligible("pull_request", pull_request_event_factory(author_association="NONE")) is False

    def test_comment_trigger(self, mock_config, issue_comment_event_factory) -> None:
        """Test that a PR comment starting with the trigger phrase is eligible."""

        mock_config()

        assert is_eligible("issue_comment", issue_comment_event_factory(body="agent review please")) is True

    def test_comment_wrong_phrase(self, mock_config, issue_comment_event_factory) -> None:
        """Test that a comment without the trigger phrase is rejected."""

        mock_config()

        assert is_eligible("issue_comment", issue_comment_event_factory(body="lgtm")) is False

    def test_comment_bot_rejected(self, mock_config, issue_comment_event_factory) -> None:
        """Test that bot-authored trigger comments are rejected."""

        mock_config()

        assert is_eligible("issue_comment", issue_comment_event_factory(sender_type="Bot")) is False

    def test_comment_non_pull_request(self, mock_config, issue_comment_event_factory) -> None:
        """Test that a comment on a plain issue is rejected."""

        mock_config()

        assert is_eligible("issue_comment", issue_comment_event_factory(is_pull_request=False)) is False

    def test_dispatch_allowed(self, mock_config) -> None:
        """Test that a manual dispatch is always eligible."""

        mock_config()

        assert is_eligible("workflow_dispatch", GithubEvent()) is True


class TestSelectBackend:
    """Test that the backend resolves from the model inputs and available credentials."""

    def test_auto_prefers_claude(self, mock_config) -> None:
        """Test that auto picks Claude when an Anthropic key is set."""

        mock_config(review_model=ReviewModel.AUTO, anthropic_api_key="key")

        assert select_backend(False) is Backend.CLAUDE

    def test_auto_selects_cursor_with_only_cursor_key(self, mock_config) -> None:
        """Test that auto picks Cursor when only a Cursor key is set."""

        mock_config(review_model=ReviewModel.AUTO, cursor_api_key="key")

        assert select_backend(False) is Backend.CURSOR

    def test_auto_none_without_creds(self, mock_config) -> None:
        """Test that auto skips when no credentials are configured."""

        mock_config(review_model=ReviewModel.AUTO)

        assert select_backend(False) is None

    def test_claude_requires_key(self, mock_config) -> None:
        """Test that an explicit Claude request skips without an Anthropic key."""

        mock_config(review_model=ReviewModel.CLAUDE)

        assert select_backend(False) is None

    def test_cursor(self, mock_config) -> None:
        """Test that an explicit Cursor request resolves with a Cursor key."""

        mock_config(review_model=ReviewModel.CURSOR, cursor_api_key="key")

        assert select_backend(False) is Backend.CURSOR

    @pytest.mark.parametrize(
        ("first_review", "expected"),
        [(True, Backend.CLAUDE), (False, Backend.CURSOR)],
        ids=["first", "subsequent"],
    )
    def test_first_review_override(self, mock_config, first_review: bool, expected: Backend) -> None:
        """Test that the first review uses first-review-model and later events use review-model."""

        mock_config(
            review_model=ReviewModel.CURSOR,
            first_review_model=ReviewModel.CLAUDE,
            anthropic_api_key="key",
            cursor_api_key="key",
        )

        assert select_backend(first_review) is expected


class TestBackends:
    """Test that each backend maps to its stream, summary, and error policy."""

    @pytest.mark.parametrize(
        ("backend", "review_text", "generate_summary", "errors"),
        [
            (Backend.CURSOR, cursor.review_text, cursor.generate_text, (CursorAgentError,)),
            (Backend.CLAUDE, claude.review_text, claude.generate_text, (anthropic.APIError,)),
        ],
        ids=["cursor", "claude"],
    )
    def test_handlers(self, backend, review_text, generate_summary, errors) -> None:
        """Test that the handler map wires the streamer, generator, and declared errors."""

        handlers = BACKENDS[backend]

        assert handlers["review_text"] is review_text
        assert handlers["generate_summary"] is generate_summary
        assert handlers["errors"] == errors


class TestBackendReviewPolicy:
    """Test that the shared backend runner owns JSONL parsing and error mapping."""

    def test_parses_backend_text_findings(self, pull_request_factory, review_inputs_factory) -> None:
        """Test that backend text is parsed into findings by the shared runner."""

        async def review_text(pr, inputs) -> AsyncIterator[str]:
            yield '{"path":"a.py","line":1,"side":"RIGHT","severity":"high","title":"A","body":"B"}\n'

        handlers = BackendHandlers(
            review_text=review_text,
            generate_summary=cursor.generate_text,
            errors=(CursorAgentError,),
            retryable=lambda exc: isinstance(exc, CursorAgentError) and exc.is_retryable,
            label="Cursor",
        )

        findings = asyncio.run(
            collect_findings(stream_backend_findings(handlers, pull_request_factory(), review_inputs_factory()))
        )

        assert [finding.title for finding in findings] == ["A"]

    @pytest.mark.parametrize("retryable", [True, False], ids=["retryable", "terminal"])
    def test_cursor_error_maps_retryability(self, pull_request_factory, review_inputs_factory, retryable: bool) -> None:
        """Test that CursorAgentError retryability is preserved by the shared runner."""

        async def review_text(pr, inputs) -> AsyncIterator[str]:
            raise CursorAgentError("bridge unavailable", is_retryable=retryable)
            yield ""

        handlers = BackendHandlers(
            review_text=review_text,
            generate_summary=cursor.generate_text,
            errors=(CursorAgentError,),
            retryable=lambda exc: isinstance(exc, CursorAgentError) and exc.is_retryable,
            label="Cursor",
        )

        with pytest.raises(ReviewBackendError) as raised:
            asyncio.run(collect_findings(stream_backend_findings(handlers, pull_request_factory(), review_inputs_factory())))

        assert raised.value.retryable is retryable
        assert "Cursor review failed" in str(raised.value)

    def test_claude_api_error_maps_retryable(self, pull_request_factory, review_inputs_factory) -> None:
        """Test that Anthropic retryability is preserved by the shared runner."""

        request = httpx.Request("GET", "https://api.anthropic.com")

        async def review_text(pr, inputs) -> AsyncIterator[str]:
            raise anthropic.APIConnectionError(request=request)
            yield ""

        handlers = BackendHandlers(
            review_text=review_text,
            generate_summary=claude.generate_text,
            errors=(anthropic.APIError,),
            retryable=lambda exc: isinstance(exc, anthropic.APIError) and claude.is_retryable_api_error(exc),
            label="Claude",
        )

        with pytest.raises(ReviewBackendError) as raised:
            asyncio.run(collect_findings(stream_backend_findings(handlers, pull_request_factory(), review_inputs_factory())))

        assert raised.value.retryable is True
        assert "Claude review failed" in str(raised.value)

    def test_unexpected_error_propagates(self, pull_request_factory, review_inputs_factory) -> None:
        """Test that undeclared backend exceptions are not converted."""

        async def review_text(pr, inputs) -> AsyncIterator[str]:
            raise ValueError("programming error")
            yield ""

        handlers = BackendHandlers(
            review_text=review_text,
            generate_summary=cursor.generate_text,
            errors=(CursorAgentError,),
            retryable=lambda exc: False,
            label="Cursor",
        )

        with pytest.raises(ValueError):
            asyncio.run(collect_findings(stream_backend_findings(handlers, pull_request_factory(), review_inputs_factory())))

    def test_empty_backend_output_raises(self, pull_request_factory, review_inputs_factory) -> None:
        """Test that an empty backend response fails instead of approving cleanly."""

        async def review_text(pr, inputs) -> AsyncIterator[str]:
            if False:
                yield ""

        handlers = BackendHandlers(
            review_text=review_text,
            generate_summary=cursor.generate_text,
            errors=(CursorAgentError,),
            retryable=lambda exc: False,
            label="Cursor",
        )

        with pytest.raises(ReviewBackendError, match="produced no output") as raised:
            asyncio.run(collect_findings(stream_backend_findings(handlers, pull_request_factory(), review_inputs_factory())))

        assert raised.value.retryable is True


class TestMain:
    """Test that main runs the round and posts a summary only on an eligible first review."""

    @pytest.mark.parametrize(
        ("action", "summary_posted"),
        [("opened", True), ("synchronize", False)],
        ids=["first-review", "later-push"],
    )
    def test_summary_gated_on_first_review(self, main_harness, action: str, summary_posted: bool) -> None:
        """Test that the summary posts on the opened event and is skipped on a later push."""

        mocks = main_harness(action=action)

        exit_code = asyncio.run(main())

        assert exit_code == 0
        mocks["run_backend_review"].assert_awaited_once_with(mocks["pr"], mocks["handlers"])

        assert mocks["post_pr_summary"].await_count == (1 if summary_posted else 0)
        if summary_posted:
            mocks["post_pr_summary"].assert_awaited_once_with(mocks["pr"], cursor.generate_text, diff="REVIEW_DIFF")

    def test_summary_skipped_when_disabled(self, main_harness) -> None:
        """Test that a first review does not post a summary when the setting is off."""

        mocks = main_harness(pr_review_summary=False)

        asyncio.run(main())

        mocks["post_pr_summary"].assert_not_awaited()

    def test_summary_skipped_when_review_fails(self, main_harness) -> None:
        """Test that a failing review round skips the summary."""

        mocks = main_harness(run_review_result=1)

        exit_code = asyncio.run(main())

        assert exit_code == 1

        mocks["post_pr_summary"].assert_not_awaited()

    def test_summary_failure_does_not_fail_the_review(self, main_harness) -> None:
        """Test that a summary error is isolated and the successful review still returns zero."""

        mocks = main_harness()
        mocks["post_pr_summary"].side_effect = SummaryGenerationError("boom")

        exit_code = asyncio.run(main())

        assert exit_code == 0

        mocks["post_pr_summary"].assert_awaited_once()

    def test_summary_backend_failure_does_not_fail_the_review(self, main_harness) -> None:
        """Test that an optional summary backend error is isolated from the review result."""

        mocks = main_harness()
        mocks["post_pr_summary"].side_effect = CursorAgentError("summary failed")

        exit_code = asyncio.run(main())

        assert exit_code == 0
        mocks["post_pr_summary"].assert_awaited_once()
