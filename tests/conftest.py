from collections.abc import Callable

import pytest

from code_review.config import CONFIG, ClaudeMode, ReviewModel
from code_review.models.shared.findings import Finding
from code_review.models.shared.github_event import GithubEvent
from code_review.models.shared.pull_request import PullRequestContext
from code_review.models.shared.severity import DiffSide, Severity
from code_review.models.shared.threads import ReviewThread, ThreadCommentAuthor, ThreadCommentNode, ThreadComments


@pytest.fixture
def mock_config(monkeypatch) -> Callable[..., None]:
    """Create a reusable settings override helper for tests."""

    def _mock_config(**overrides) -> None:
        defaults = {
            "github_token": "test-token",
            "anthropic_api_key": "",
            "cursor_api_key": "",
            "claude_routine_api_key": "",
            "claude_routine_id": None,
            "review_model": ReviewModel.AUTO,
            "first_review_model": None,
            "claude_mode": ClaudeMode.API,
            "claude_model": "claude-opus-4-8",
            "cursor_model": "composer-2.5",
            "additional_context": "",
            "approval_include": frozenset({Severity.CRITICAL}),
            "approval_disable": False,
            "min_severity": Severity.LOW,
            "low_findings_cap": 3,
            "max_findings": None,
            "include_paths": (),
            "exclude_paths": (),
            "trigger_phrase": "agent review",
            "review_drafts": True,
            "author_associations": (),
            "pr_number": None,
        }
        for key, value in {**defaults, **overrides}.items():
            monkeypatch.setattr(f"code_review.config.SETTINGS.{key}", value)

    _mock_config()

    return _mock_config


@pytest.fixture
def finding_factory() -> Callable[..., Finding]:
    """Build Finding instances with sensible defaults."""

    def _build(**overrides) -> Finding:
        defaults = {
            "path": "src/app.py",
            "line": 10,
            "side": DiffSide.RIGHT,
            "severity": Severity.HIGH,
            "title": "Off-by-one error",
            "body": "The loop overruns the array.",
        }

        return Finding(**{**defaults, **overrides})

    return _build


@pytest.fixture
def pull_request_factory() -> Callable[..., PullRequestContext]:
    """Build PullRequestContext instances with sensible defaults."""

    def _build(**overrides) -> PullRequestContext:
        defaults = {
            "repo": "octo/repo",
            "number": 7,
            "head_sha": "abc123",
            "head_ref": "feature",
            "url": "https://github.com/octo/repo/pull/7",
            "author": "dev",
            "is_draft": False,
            "state": "OPEN",
        }

        return PullRequestContext(**{**defaults, **overrides})

    return _build


@pytest.fixture
def thread_comment_factory() -> Callable[..., ThreadCommentNode]:
    """Build a review-thread first comment carrying a runner marker."""

    def _build(
        *,
        title: str = "Off-by-one error",
        severity: str = "Critical",
        marker: str = CONFIG["review_marker"],
        author: str = "github-actions[bot]",
        path: str = "src/app.py",
        body: str | None = None,
    ) -> ThreadCommentNode:
        resolved_body = body if body is not None else f"### {title}\n\n**{severity} Severity**\n\nDetail.\n\n{marker}"

        return ThreadCommentNode(author=ThreadCommentAuthor(login=author), body=resolved_body, path=path)

    return _build


@pytest.fixture
def review_thread_factory(thread_comment_factory) -> Callable[..., ReviewThread]:
    """Build a ReviewThread wrapping a single first comment."""

    def _build(*, id: str = "thread-1", is_resolved: bool = False, is_outdated: bool = False, **comment_kwargs) -> ReviewThread:
        comment = thread_comment_factory(**comment_kwargs)

        return ReviewThread(
            id=id, is_resolved=is_resolved, is_outdated=is_outdated, comments=ThreadComments(nodes=[comment])
        )

    return _build


@pytest.fixture
def pull_request_event_factory() -> Callable[..., GithubEvent]:
    """Build a `pull_request` GithubEvent with overridable eligibility fields."""

    def _build(
        *,
        action: str = "opened",
        author_association: str = "MEMBER",
        head_full_name: str = "octo/repo",
        sender_type: str = "User",
        draft: bool = False,
        number: int = 7,
    ) -> GithubEvent:
        return GithubEvent.model_validate(
            {
                "action": action,
                "pull_request": {
                    "number": number,
                    "draft": draft,
                    "author_association": author_association,
                    "head": {"repo": {"full_name": head_full_name}, "sha": "abc123", "ref": "feature"},
                },
                "sender": {"type": sender_type},
            }
        )

    return _build


@pytest.fixture
def issue_comment_event_factory() -> Callable[..., GithubEvent]:
    """Build an `issue_comment` GithubEvent with overridable eligibility fields."""

    def _build(
        *,
        body: str = "agent review please",
        author_association: str = "MEMBER",
        sender_type: str = "User",
        is_pull_request: bool = True,
        number: int = 7,
        comment_id: int = 555,
    ) -> GithubEvent:
        return GithubEvent.model_validate(
            {
                "action": "created",
                "issue": {"number": number, "pull_request": {"url": "x"} if is_pull_request else None},
                "comment": {"id": comment_id, "body": body, "author_association": author_association},
                "sender": {"type": sender_type},
            }
        )

    return _build
