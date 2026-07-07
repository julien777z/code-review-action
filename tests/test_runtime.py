import asyncio
from unittest.mock import AsyncMock

import pytest

from code_review.config import ReviewModel
from code_review.models.shared.github_event import GithubEvent
from code_review.review_backends import claude, cursor
from code_review.summary import SummaryGenerationError
from code_review.runtime import (
    BACKENDS,
    Backend,
    association_allowed,
    is_eligible,
    is_first_review_event,
    main,
    reaction_subject,
    resolve_pr_number,
    select_backend,
)


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

    def test_auto_falls_back_to_cursor(self, mock_config) -> None:
        """Test that auto falls back to Cursor when only a Cursor key is set."""

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
    """Test that each backend maps to its review runner and summary generator."""

    @pytest.mark.parametrize(
        ("backend", "run_review", "generate_summary"),
        [
            (Backend.CURSOR, cursor.run_cursor_review, cursor.generate_text),
            (Backend.CLAUDE, claude.run_claude_api_review, claude.generate_text),
        ],
        ids=["cursor", "claude"],
    )
    def test_handlers(self, backend, run_review, generate_summary) -> None:
        """Test that the handler map wires the runner and generator for a backend."""

        handlers = BACKENDS[backend]

        assert handlers["run_review"] is run_review
        assert handlers["generate_summary"] is generate_summary


class TestMain:
    """Test that main runs the round and posts a summary only on an eligible first review."""

    @pytest.mark.parametrize(
        ("action", "summary_posted"),
        [("opened", True), ("synchronize", False)],
        ids=["first-review", "later-push"],
    )
    def test_summary_gated_on_first_review(
        self, monkeypatch, mock_config, pull_request_event_factory, pull_request_factory, action: str, summary_posted: bool
    ) -> None:
        """Test that the summary posts on the opened event and is skipped on a later push."""

        mock_config(review_model=ReviewModel.CURSOR, cursor_api_key="key", pr_review_summary=True)
        run_review = AsyncMock(return_value=0)
        post_pr_summary = AsyncMock(return_value=None)
        generator = cursor.generate_text
        monkeypatch.setattr(
            "code_review.runtime.BACKENDS",
            {Backend.CURSOR: {"run_review": run_review, "generate_summary": generator}},
        )
        monkeypatch.setattr("code_review.runtime.post_pr_summary", post_pr_summary)
        monkeypatch.setattr("code_review.runtime.add_reaction", AsyncMock(return_value=None))
        monkeypatch.setattr("code_review.runtime.remove_reaction", AsyncMock(return_value=None))
        pr = pull_request_factory()
        monkeypatch.setattr("code_review.runtime.fetch_pull_request", AsyncMock(return_value=pr))
        monkeypatch.setattr(
            "code_review.runtime.load_event",
            lambda: ("pull_request", pull_request_event_factory(action=action)),
        )

        exit_code = asyncio.run(main())

        assert exit_code == 0
        run_review.assert_awaited_once_with(pr)

        assert post_pr_summary.await_count == (1 if summary_posted else 0)
        if summary_posted:
            post_pr_summary.assert_awaited_once_with(pr, generator)

    def test_summary_skipped_when_disabled(
        self, monkeypatch, mock_config, pull_request_event_factory, pull_request_factory
    ) -> None:
        """Test that a first review does not post a summary when the setting is off."""

        mock_config(review_model=ReviewModel.CURSOR, cursor_api_key="key", pr_review_summary=False)
        post_pr_summary = AsyncMock(return_value=None)
        monkeypatch.setattr(
            "code_review.runtime.BACKENDS",
            {Backend.CURSOR: {"run_review": AsyncMock(return_value=0), "generate_summary": cursor.generate_text}},
        )
        monkeypatch.setattr("code_review.runtime.post_pr_summary", post_pr_summary)
        monkeypatch.setattr("code_review.runtime.add_reaction", AsyncMock(return_value=None))
        monkeypatch.setattr("code_review.runtime.remove_reaction", AsyncMock(return_value=None))
        monkeypatch.setattr("code_review.runtime.fetch_pull_request", AsyncMock(return_value=pull_request_factory()))
        monkeypatch.setattr(
            "code_review.runtime.load_event",
            lambda: ("pull_request", pull_request_event_factory(action="opened")),
        )

        asyncio.run(main())

        post_pr_summary.assert_not_awaited()

    def test_summary_skipped_when_review_fails(
        self, monkeypatch, mock_config, pull_request_event_factory, pull_request_factory
    ) -> None:
        """Test that a failing review round skips the summary."""

        mock_config(review_model=ReviewModel.CURSOR, cursor_api_key="key", pr_review_summary=True)
        post_pr_summary = AsyncMock(return_value=None)
        monkeypatch.setattr(
            "code_review.runtime.BACKENDS",
            {Backend.CURSOR: {"run_review": AsyncMock(return_value=1), "generate_summary": cursor.generate_text}},
        )
        monkeypatch.setattr("code_review.runtime.post_pr_summary", post_pr_summary)
        monkeypatch.setattr("code_review.runtime.add_reaction", AsyncMock(return_value=None))
        monkeypatch.setattr("code_review.runtime.remove_reaction", AsyncMock(return_value=None))
        monkeypatch.setattr("code_review.runtime.fetch_pull_request", AsyncMock(return_value=pull_request_factory()))
        monkeypatch.setattr(
            "code_review.runtime.load_event",
            lambda: ("pull_request", pull_request_event_factory(action="opened")),
        )

        exit_code = asyncio.run(main())

        assert exit_code == 1

        post_pr_summary.assert_not_awaited()

    def test_summary_failure_does_not_fail_the_review(
        self, monkeypatch, mock_config, pull_request_event_factory, pull_request_factory
    ) -> None:
        """Test that a summary error is isolated and the successful review still returns zero."""

        mock_config(review_model=ReviewModel.CURSOR, cursor_api_key="key", pr_review_summary=True)
        post_pr_summary = AsyncMock(side_effect=SummaryGenerationError("boom"))
        monkeypatch.setattr(
            "code_review.runtime.BACKENDS",
            {Backend.CURSOR: {"run_review": AsyncMock(return_value=0), "generate_summary": cursor.generate_text}},
        )
        monkeypatch.setattr("code_review.runtime.post_pr_summary", post_pr_summary)
        monkeypatch.setattr("code_review.runtime.add_reaction", AsyncMock(return_value=None))
        monkeypatch.setattr("code_review.runtime.remove_reaction", AsyncMock(return_value=None))
        monkeypatch.setattr("code_review.runtime.fetch_pull_request", AsyncMock(return_value=pull_request_factory()))
        monkeypatch.setattr(
            "code_review.runtime.load_event",
            lambda: ("pull_request", pull_request_event_factory(action="opened")),
        )

        exit_code = asyncio.run(main())

        assert exit_code == 0

        post_pr_summary.assert_awaited_once()
