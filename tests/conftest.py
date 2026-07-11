import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from code_review.config import CONFIG, Settings
from code_review.errors import ReviewBackendError
from code_review.models.backend import (
    Backend,
    BackendHandlers,
    FindingsBackend,
    FindingsSession,
    GetFindingsSession,
    OpenReviewSession,
    ReviewSessionStreams,
)
from code_review.models.config import ReviewModel
from code_review.models.findings import Finding, FindingCategory
from code_review.models.github_event import GithubEvent
from code_review.models.pull_request import PullRequestContext, ReviewInputs
from code_review.models.review import FlushCompletion, ReviewRoundResult, RoundFindings
from code_review.models.severity import DiffSide, Severity
from code_review.models.threads import ReviewThread, ThreadCommentAuthor, ThreadCommentNode, ThreadComments
from code_review.review import findings
from code_review.review_backends import claude


class SessionState(BaseModel):
    """Observable lifecycle counters of a fake findings session."""

    opened: int = 0
    closed: int = 0
    flush_calls: int = 0


@pytest.fixture
def mock_config(monkeypatch) -> Callable[..., None]:
    """Create a reusable settings override helper for tests."""

    def _mock_config(**overrides) -> None:
        defaults = {
            "github_token": "test-token",
            "resolve_token": "",
            "claude_code_oauth_token": "",
            "codex_auth_json": "",
            "review_model": ReviewModel.AUTO,
            "claude_model": "claude-opus-4-8",
            "codex_model": "gpt-5.6-terra",
            "fallback_on_usage_limit": True,
            "additional_context": "",
            "approval_include": frozenset({Severity.CRITICAL}),
            "approval_disable": False,
            "pr_review_summary": True,
            "enforce_project_rules": True,
            "project_rules_severity": None,
            "simplify_suggest": False,
            "simplify_suggest_severity": None,
            "simplify_nearby_code": False,
            "min_severity": Severity.LOW,
            "low_findings_cap": 3,
            "max_findings": None,
            "include_paths": (),
            "exclude_paths": (),
            "trigger_phrase": "agent review",
            "review_drafts": True,
            "pr_number": None,
            "review_timeout_minutes": 15,
        }
        for key, value in {**defaults, **overrides}.items():
            monkeypatch.setattr(f"code_review.config.SETTINGS.{key}", value)

    _mock_config()

    return _mock_config


@pytest.fixture
def override_review_timeout(monkeypatch) -> Callable[[timedelta | None], None]:
    """Override the computed review timeout with an explicit duration for tests."""

    def _override(duration: timedelta | None) -> None:
        monkeypatch.setattr(Settings, "review_timeout", property(lambda self: duration))

    return _override


@pytest.fixture
def post_comment_mock(monkeypatch) -> AsyncMock:
    """Patch the inline-comment poster in the findings module and return the mock."""

    mock = AsyncMock(return_value=True)
    monkeypatch.setattr("code_review.review.findings.post_comment", mock)

    return mock


@pytest.fixture
def round_findings_factory() -> Callable[..., RoundFindings]:
    """Build RoundFindings instances with sensible defaults."""

    def _build(**overrides) -> RoundFindings:
        defaults = {"posted_any": False, "timed_out": False}

        return RoundFindings(**{**defaults, **overrides})

    return _build


@pytest.fixture
def finding_factory() -> Callable[..., Finding]:
    """Build Finding instances with sensible defaults."""

    def _build(**overrides) -> Finding:
        defaults = {
            "path": "src/app.py",
            "line": 10,
            "side": DiffSide.RIGHT,
            "category": FindingCategory.BUG,
            "severity": Severity.HIGH,
            "title": "Off-by-one error",
            "body": "The loop overruns the array.",
        }

        return Finding(**{**defaults, **overrides})

    return _build


@pytest.fixture
def review_inputs_factory(pull_request_factory) -> Callable[..., ReviewInputs]:
    """Build ReviewInputs with a default PR and empty diff."""

    def _build(**overrides) -> ReviewInputs:
        defaults = {"pr": pull_request_factory(), "diff": "", "posted_findings": {}}

        return ReviewInputs(**{**defaults, **overrides})

    return _build


@pytest.fixture
def review_session_opener_factory() -> Callable[..., "OpenReviewSession"]:
    """Build an OpenReviewSession double from scripted review and flush text streams."""

    def _build(
        review_stream: Callable[[], AsyncIterator[str]],
        flush_stream: Callable[[], AsyncIterator[str]] | None = None,
    ) -> "OpenReviewSession":
        async def _empty() -> AsyncIterator[str]:
            return
            yield ""

        @asynccontextmanager
        async def _open(pr: PullRequestContext, inputs: ReviewInputs) -> AsyncIterator[ReviewSessionStreams]:
            yield ReviewSessionStreams(review_text=review_stream, flush_text=flush_stream or _empty)

        return _open

    return _build


@pytest.fixture
def zero_flush_headroom(monkeypatch) -> None:
    """Remove the flush posting headroom so short test budgets still fund a flush window."""

    monkeypatch.setitem(findings.FLUSH_TIMING, "posting_headroom", timedelta(0))


@pytest.fixture
def findings_session_factory(monkeypatch) -> Callable[..., tuple[tuple[FindingsBackend, ...], SessionState]]:
    """Build a scriptable findings-session double for the two-phase review orchestration."""

    monkeypatch.setattr("code_review.review.findings.REVIEW_RETRY_BACKOFF", timedelta(0))

    def _build(
        review_findings: list[Finding] | None = None,
        *,
        block_after_review: bool = False,
        review_error: ReviewBackendError | None = None,
        review_failures: int = 0,
        flush_findings: list[Finding] | None = None,
        flush_error: ReviewBackendError | None = None,
        block_in_flush: bool = False,
        flush_complete: bool = False,
    ) -> tuple[tuple[FindingsBackend, ...], SessionState]:
        state = SessionState()

        @asynccontextmanager
        async def _open(inputs: ReviewInputs) -> AsyncIterator[FindingsSession]:
            state.opened += 1
            attempt = state.opened
            completion = FlushCompletion()

            async def _findings() -> AsyncIterator[Finding]:
                if review_error is not None and (review_failures == 0 or attempt <= review_failures):
                    raise review_error

                for finding in review_findings or []:
                    yield finding

                if block_after_review:
                    await asyncio.Event().wait()

            async def _flush_findings() -> AsyncIterator[Finding]:
                state.flush_calls += 1
                if flush_error is not None:
                    raise flush_error

                for finding in flush_findings or []:
                    yield finding

                if block_in_flush:
                    await asyncio.Event().wait()

                completion.complete = flush_complete

            try:
                yield FindingsSession(findings=_findings, flush_findings=_flush_findings, flush_completion=completion)
            finally:
                state.closed += 1

        return (FindingsBackend(label="Test", open_session=_open),), state

    return _build


@pytest.fixture
def review_github_mocks(monkeypatch) -> dict[str, AsyncMock]:
    """Patch the GitHub seams run_review_round calls and return the mocks for assertion."""

    mocks = {
        "already_reviewed": AsyncMock(return_value=False),
        "head_check_concluded": AsyncMock(return_value=False),
        "current_head_sha": AsyncMock(return_value="abc123"),
        "pull_request_diff_if_available": AsyncMock(return_value=""),
        "diff_anchors": AsyncMock(return_value=({}, set())),
        "list_review_threads": AsyncMock(return_value=[]),
        "start_check_run": AsyncMock(return_value="check-1"),
        "complete_check_run": AsyncMock(return_value=True),
        "post_comment": AsyncMock(return_value=True),
        "post_review": AsyncMock(return_value=True),
        "resolve_threads": AsyncMock(return_value=None),
    }
    review_names = (
        "already_reviewed",
        "head_check_concluded",
        "current_head_sha",
        "pull_request_diff_if_available",
        "diff_anchors",
        "list_review_threads",
        "start_check_run",
        "complete_check_run",
        "post_review",
        "resolve_threads",
    )
    for name in review_names:
        monkeypatch.setattr(f"code_review.review.round.{name}", mocks[name])
    monkeypatch.setattr("code_review.review.threads.list_review_threads", mocks["list_review_threads"])
    monkeypatch.setattr("code_review.review.findings.post_comment", mocks["post_comment"])

    return mocks


@pytest.fixture
def summary_github_mocks(monkeypatch) -> dict[str, AsyncMock]:
    """Patch the GitHub seams post_pr_summary calls and return the mocks for assertion."""

    mocks = {
        "current_head_sha": AsyncMock(return_value="abc123"),
        "pull_request_diff_if_available": AsyncMock(return_value="DIFF_BODY"),
        "pull_request_body": AsyncMock(return_value=""),
        "update_pull_request_body": AsyncMock(return_value=None),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(f"code_review.summary.{name}", mock)

    return mocks


@pytest.fixture
def main_harness(monkeypatch, mock_config, pull_request_factory, pull_request_event_factory) -> Callable[..., dict[str, AsyncMock | PullRequestContext]]:
    """Patch the seams main() drives and return the mocks; set the event action and review result per call."""

    def _setup(
        *, action: str = "opened", run_review_result: int = 0, **config_overrides
    ) -> dict[str, AsyncMock | PullRequestContext]:
        mock_config(review_model=ReviewModel.CLAUDE, claude_code_oauth_token="token", **config_overrides)

        run_backend_review = AsyncMock(return_value=ReviewRoundResult(exit_code=run_review_result, diff="REVIEW_DIFF"))
        post_pr_summary = AsyncMock(return_value=None)
        pr = pull_request_factory()
        handlers = BackendHandlers(
            review_session=claude.review_session,
            generate_summary=claude.generate_text,
            label="Claude",
        )

        monkeypatch.setattr(
            "code_review.runtime.BACKENDS",
            {Backend.CLAUDE: handlers},
        )
        monkeypatch.setattr("code_review.runtime.run_backend_review", run_backend_review)
        monkeypatch.setattr("code_review.runtime.post_pr_summary", post_pr_summary)
        monkeypatch.setattr("code_review.runtime.add_reaction", AsyncMock(return_value=None))
        monkeypatch.setattr("code_review.runtime.remove_reaction", AsyncMock(return_value=None))
        monkeypatch.setattr("code_review.runtime.fetch_pull_request", AsyncMock(return_value=pr))
        monkeypatch.setattr(
            "code_review.runtime.load_event",
            lambda: ("pull_request", pull_request_event_factory(action=action)),
        )

        return {"handlers": handlers, "run_backend_review": run_backend_review, "post_pr_summary": post_pr_summary, "pr": pr}

    return _setup


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
        category: str = "Bug",
        severity: str = "Critical",
        marker: str = CONFIG["review_marker"],
        author: str = "github-actions[bot]",
        path: str = "src/app.py",
        body: str | None = None,
    ) -> ThreadCommentNode:
        resolved_body = body
        if resolved_body is None:
            resolved_body = (
                f"{CONFIG['untrusted_input_open']}\n"
                f"### {title}\n\n**{severity} Severity**<br><sub>{category}</sub>\n\nDetail.\n"
                f"{CONFIG['untrusted_input_close']}\n\n"
                f"{marker}"
            )

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
        sender_type: str = "User",
        is_pull_request: bool = True,
        number: int = 7,
        comment_id: int = 555,
    ) -> GithubEvent:
        return GithubEvent.model_validate(
            {
                "action": "created",
                "issue": {"number": number, "pull_request": {"url": "x"} if is_pull_request else None},
                "comment": {"id": comment_id, "body": body},
                "sender": {"type": sender_type},
            }
        )

    return _build
