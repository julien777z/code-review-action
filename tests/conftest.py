import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from cursor_sdk import AgentBusyError

from code_review.config import CONFIG, Settings
from code_review.errors import ReviewBackendError
from code_review.models.backend import (
    Backend,
    BackendHandlers,
    FindingsSession,
    GetBackendFindings,
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
from code_review.review_backends import cursor


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
            "anthropic_api_key": "",
            "cursor_api_key": "",
            "review_model": ReviewModel.AUTO,
            "first_review_model": None,
            "claude_model": "claude-opus-4-8",
            "cursor_model": "composer-2.5",
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
def flaky_stream_factory(monkeypatch) -> Callable[..., tuple[GetBackendFindings, list[int]]]:
    """Build a streaming get_findings double that fails a set number of times before yielding findings."""

    monkeypatch.setattr("code_review.review.findings.REVIEW_RETRY_BACKOFF", timedelta(0))

    def _build(
        *,
        failures: int,
        error: ReviewBackendError,
        result: list[Finding] | None = None,
        yield_before_error: bool = False,
    ) -> tuple[GetBackendFindings, list[int]]:
        calls: list[int] = []
        findings = result if result is not None else []

        async def _get_findings(inputs: ReviewInputs) -> AsyncIterator[Finding]:
            calls.append(len(calls))
            if len(calls) <= failures:
                if yield_before_error and findings:
                    yield findings[0]

                raise error

            for finding in findings:
                yield finding

        return _get_findings, calls

    return _build


@pytest.fixture
def stream_findings_factory() -> Callable[[list[Finding]], GetBackendFindings]:
    """Build a streaming get_findings double that yields a fixed list of findings."""

    def _build(findings: list[Finding]) -> GetBackendFindings:
        async def _get_findings(inputs: ReviewInputs) -> AsyncIterator[Finding]:
            for finding in findings:
                yield finding

        return _get_findings

    return _build


@pytest.fixture
def blocking_stream_factory() -> Callable[[list[Finding]], tuple[GetBackendFindings, dict[str, bool]]]:
    """Build a get_findings double that yields findings then blocks until cancelled, recording its cleanup."""

    def _build(findings: list[Finding]) -> tuple[GetBackendFindings, dict[str, bool]]:
        state = {"cleaned_up": False}

        async def _get_findings(inputs: ReviewInputs) -> AsyncIterator[Finding]:
            try:
                for finding in findings:
                    yield finding

                await asyncio.Event().wait()
            finally:
                state["cleaned_up"] = True

        return _get_findings, state

    return _build


@pytest.fixture
def cursor_agent_factory() -> Callable[..., tuple[MagicMock, list[MagicMock]]]:
    """Build a fake Cursor AsyncAgent whose review and flush runs stream scripted chunks."""

    async def _chunks(parts: tuple[str, ...]) -> AsyncIterator[str]:
        for part in parts:
            yield part

    def _build(
        *,
        review_chunks: tuple[str, ...] = (),
        flush_chunks: tuple[str, ...] = (),
        busy_sends: int = 0,
    ) -> tuple[MagicMock, list[MagicMock]]:
        def _make_run(parts: tuple[str, ...]) -> MagicMock:
            run = MagicMock()
            run.cancel = AsyncMock(return_value=None)
            run.iter_text = lambda: _chunks(parts)

            return run

        runs = [_make_run(review_chunks), _make_run(flush_chunks)]
        send_results: list[object] = [runs[0]]
        send_results.extend(AgentBusyError("agent busy") for _ in range(busy_sends))
        send_results.append(runs[1])

        agent = MagicMock()
        agent.send = AsyncMock(side_effect=send_results)
        agent.close = AsyncMock(return_value=None)

        return agent, runs

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
def findings_session_factory(monkeypatch) -> Callable[..., tuple[GetFindingsSession, SessionState]]:
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
    ) -> tuple[GetFindingsSession, SessionState]:
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

        return _open, state

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
def anthropic_client_factory() -> Callable[..., MagicMock]:
    """Build an AsyncAnthropic-style async context manager whose messages.create returns text blocks."""

    def _build(text: str = "Summary text") -> MagicMock:
        block = MagicMock()
        block.type = "text"
        block.text = text
        message = MagicMock()
        message.content = [block]
        client = MagicMock()
        client.messages.create = AsyncMock(return_value=message)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        return client

    return _build


class FakeManagedStream:
    """An async context manager and async iterator over a fixed list of Managed Agents events."""

    def __init__(self, events: list[MagicMock]) -> None:
        self._events = list(events)

    async def __aenter__(self) -> "FakeManagedStream":
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def __aiter__(self) -> "FakeManagedStream":
        return self

    async def __anext__(self) -> MagicMock:
        if not self._events:
            raise StopAsyncIteration

        return self._events.pop(0)


@pytest.fixture
def managed_agent_event_factory() -> Callable[..., MagicMock]:
    """Build Managed Agents stream events (agent.message, idle, terminated) as SDK-shaped doubles."""

    def _build(event_type: str, *, text: str | None = None, stop_reason: str | None = None) -> MagicMock:
        event = MagicMock()
        event.type = event_type
        if event_type == "agent.message":
            block = MagicMock()
            block.type = "text"
            block.text = text
            event.content = [block]
        if event_type == "session.status_idle":
            event.stop_reason = MagicMock()
            event.stop_reason.type = stop_reason

        return event

    return _build


@pytest.fixture
def managed_agent_client_factory() -> Callable[..., MagicMock]:
    """Build an AsyncAnthropic double driving Managed Agents turns, one event list per opened stream."""

    def _build(events: list[MagicMock], *turn_events: list[MagicMock]) -> MagicMock:
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        environment = MagicMock()
        environment.id = "env-created"
        client.beta.environments.create = AsyncMock(return_value=environment)
        client.beta.environments.delete = AsyncMock(return_value=None)

        agent = MagicMock()
        agent.id = "agent-1"
        agent.version = "v1"
        client.beta.agents.create = AsyncMock(return_value=agent)
        client.beta.agents.archive = AsyncMock(return_value=None)

        session = MagicMock()
        session.id = "session-1"
        client.beta.sessions.create = AsyncMock(return_value=session)
        client.beta.sessions.delete = AsyncMock(return_value=None)
        client.beta.sessions.events.send = AsyncMock(return_value=None)
        client.beta.sessions.events.stream = AsyncMock(
            side_effect=[FakeManagedStream(turn) for turn in (events, *turn_events)]
        )

        return client

    return _build


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
        mock_config(review_model=ReviewModel.CURSOR, cursor_api_key="key", **config_overrides)

        run_backend_review = AsyncMock(return_value=ReviewRoundResult(exit_code=run_review_result, diff="REVIEW_DIFF"))
        post_pr_summary = AsyncMock(return_value=None)
        pr = pull_request_factory()
        handlers = BackendHandlers(
            review_session=cursor.review_session,
            generate_summary=cursor.generate_text,
            errors=(cursor.CursorAgentError,),
            retryable=lambda exc: isinstance(exc, cursor.CursorAgentError) and exc.is_retryable,
            label="Cursor",
        )

        monkeypatch.setattr(
            "code_review.runtime.BACKENDS",
            {Backend.CURSOR: handlers},
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
