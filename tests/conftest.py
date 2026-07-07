from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from code_review.config import CONFIG, ReviewModel
from code_review.models.shared.findings import Finding
from code_review.models.shared.github_event import GithubEvent
from code_review.models.shared.pull_request import PullRequestContext, ReviewInputs
from code_review.models.shared.severity import DiffSide, Severity
from code_review.models.shared.threads import ReviewThread, ThreadCommentAuthor, ThreadCommentNode, ThreadComments
from code_review.review import GetFindings, ReviewBackendError
from code_review.review_backends import cursor
from code_review.runtime import Backend


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
            "claude_environment_id": "",
            "additional_context": "",
            "approval_include": frozenset({Severity.CRITICAL}),
            "approval_disable": False,
            "pr_review_summary": True,
            "enforce_project_rules": True,
            "simplify_suggest": False,
            "simplify_nearby_code": False,
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
def review_inputs_factory(pull_request_factory) -> Callable[..., ReviewInputs]:
    """Build ReviewInputs with a default PR and empty diff."""

    def _build(**overrides) -> ReviewInputs:
        defaults = {"pr": pull_request_factory(), "diff": "", "posted_findings": {}}

        return ReviewInputs(**{**defaults, **overrides})

    return _build


@pytest.fixture
def flaky_stream_factory(monkeypatch) -> Callable[..., tuple[GetFindings, list[int]]]:
    """Build a streaming get_findings double that fails a set number of times before yielding findings."""

    monkeypatch.setattr("code_review.review.REVIEW_RETRY_BACKOFF", timedelta(0))

    def _build(
        *,
        failures: int,
        error: ReviewBackendError,
        result: list[Finding] | None = None,
        yield_before_error: bool = False,
    ) -> tuple[GetFindings, list[int]]:
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
def stream_findings_factory() -> Callable[[list[Finding]], GetFindings]:
    """Build a streaming get_findings double that yields a fixed list of findings."""

    def _build(findings: list[Finding]) -> GetFindings:
        async def _get_findings(inputs: ReviewInputs) -> AsyncIterator[Finding]:
            for finding in findings:
                yield finding

        return _get_findings

    return _build


@pytest.fixture
def review_github_mocks(monkeypatch) -> dict[str, AsyncMock]:
    """Patch the GitHub seams run_review_round calls and return the mocks for assertion."""

    mocks = {
        "already_reviewed": AsyncMock(return_value=False),
        "head_check_concluded": AsyncMock(return_value=False),
        "current_head_sha": AsyncMock(return_value="abc123"),
        "pull_request_diff": AsyncMock(return_value=""),
        "diff_anchors": AsyncMock(return_value=({}, set())),
        "list_review_threads": AsyncMock(return_value=[]),
        "start_check_run": AsyncMock(return_value="check-1"),
        "complete_check_run": AsyncMock(return_value=True),
        "post_comment": AsyncMock(return_value=True),
        "post_review": AsyncMock(return_value=True),
        "resolve_threads": AsyncMock(return_value=None),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(f"code_review.review.{name}", mock)

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
    """Build an AsyncAnthropic double driving a Managed Agents session over the given events."""

    def _build(events: list[MagicMock]) -> MagicMock:
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
        client.beta.sessions.events.stream = AsyncMock(return_value=FakeManagedStream(events))

        return client

    return _build


@pytest.fixture
def summary_github_mocks(monkeypatch) -> dict[str, AsyncMock]:
    """Patch the GitHub seams post_pr_summary calls and return the mocks for assertion."""

    mocks = {
        "current_head_sha": AsyncMock(return_value="abc123"),
        "pull_request_diff": AsyncMock(return_value="DIFF_BODY"),
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

        run_review = AsyncMock(return_value=run_review_result)
        post_pr_summary = AsyncMock(return_value=None)
        pr = pull_request_factory()

        monkeypatch.setattr(
            "code_review.runtime.BACKENDS",
            {Backend.CURSOR: {"run_review": run_review, "generate_summary": cursor.generate_text}},
        )
        monkeypatch.setattr("code_review.runtime.post_pr_summary", post_pr_summary)
        monkeypatch.setattr("code_review.runtime.add_reaction", AsyncMock(return_value=None))
        monkeypatch.setattr("code_review.runtime.remove_reaction", AsyncMock(return_value=None))
        monkeypatch.setattr("code_review.runtime.fetch_pull_request", AsyncMock(return_value=pr))
        monkeypatch.setattr(
            "code_review.runtime.load_event",
            lambda: ("pull_request", pull_request_event_factory(action=action)),
        )

        return {"run_review": run_review, "post_pr_summary": post_pr_summary, "pr": pr}

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
