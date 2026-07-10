import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from cursor_sdk import AgentBusyError, UnsupportedRunOperationError

from code_review.prompt import flush_prompt
from code_review.review_backends import cursor


async def chunk_stream(*parts: str) -> AsyncIterator[str]:
    """Yield the given text parts as an async chunk stream."""

    for part in parts:
        yield part


async def collect(chunks: AsyncIterator[str]) -> list[str]:
    """Drain an async chunk stream into a list."""

    return [chunk async for chunk in chunks]


class TestGenerateText:
    """Test that the single-shot Cursor completion joins the streamed chunks."""

    def test_joins_chunks(self, monkeypatch, mock_config) -> None:
        """Test that the streamed chunks are concatenated into one string."""

        mock_config(cursor_api_key="key")
        monkeypatch.setattr(
            "code_review.review_backends.cursor.run_agent",
            lambda prompt: chunk_stream("Gen", "erated ", "summary"),
        )

        assert asyncio.run(cursor.generate_text("prompt")) == "Generated summary"

    def test_summary_turn_does_not_load_project_rules(
        self, monkeypatch, mock_config, cursor_agent_factory
    ) -> None:
        """Test that the summary turn runs a local agent without loading the project settings."""

        mock_config(cursor_api_key="key")
        agent, _ = cursor_agent_factory(review_chunks=("ok",))
        recorded: dict[str, bool] = {}

        async def _create_agent(*, load_project_rules: bool) -> MagicMock:
            recorded["load_project_rules"] = load_project_rules

            return agent

        monkeypatch.setattr("code_review.review_backends.cursor.create_agent", _create_agent)

        asyncio.run(cursor.generate_text("prompt"))

        assert recorded["load_project_rules"] is False


class TestReviewSession:
    """Test that the review session streams the review turn and can interrupt into a flush turn."""

    @pytest.mark.parametrize(
        ("enforce", "nearby", "loads_rules"),
        [(True, False, True), (False, True, True), (False, False, False)],
        ids=["enforcing", "nearby-code", "neither"],
    )
    def test_session_loads_rules_per_enforcement(
        self,
        monkeypatch,
        mock_config,
        cursor_agent_factory,
        pull_request_factory,
        review_inputs_factory,
        enforce: bool,
        nearby: bool,
        loads_rules: bool,
    ) -> None:
        """Test that the session agent loads project rules when review criteria need repo context."""

        mock_config(cursor_api_key="key", enforce_project_rules=enforce, simplify_nearby_code=nearby)
        agent, _ = cursor_agent_factory(review_chunks=("NO_FINDINGS",))
        recorded: dict[str, bool] = {}

        async def _create_agent(*, load_project_rules: bool) -> MagicMock:
            recorded["load_project_rules"] = load_project_rules

            return agent

        monkeypatch.setattr("code_review.review_backends.cursor.create_agent", _create_agent)

        async def run() -> list[str]:
            async with cursor.review_session(pull_request_factory(), review_inputs_factory()) as session:
                return await collect(session["review_text"]())

        chunks = asyncio.run(run())

        assert chunks == ["NO_FINDINGS"]
        assert recorded["load_project_rules"] is loads_rules

    def test_flush_cancels_the_run_then_sends_the_flush_prompt(
        self, monkeypatch, mock_config, cursor_agent_factory, pull_request_factory, review_inputs_factory
    ) -> None:
        """Test that the flush turn cancels the in-flight run and sends the wrap-up prompt to the same agent."""

        mock_config(cursor_api_key="key")
        agent, runs = cursor_agent_factory(review_chunks=("partial",), flush_chunks=("late",))
        monkeypatch.setattr(
            "code_review.review_backends.cursor.create_agent", AsyncMock(return_value=agent)
        )

        async def run() -> list[str]:
            async with cursor.review_session(pull_request_factory(), review_inputs_factory()) as session:
                return await collect(session["flush_text"]())

        chunks = asyncio.run(run())

        assert chunks == ["late"]
        runs[0].cancel.assert_awaited_once()
        assert agent.send.await_args_list[1].args[0] == flush_prompt()
        agent.close.assert_awaited_once()

    def test_flush_tolerates_an_already_finished_run(
        self, monkeypatch, mock_config, cursor_agent_factory, pull_request_factory, review_inputs_factory
    ) -> None:
        """Test that a run that already reached a terminal status does not block the flush turn."""

        mock_config(cursor_api_key="key")
        agent, runs = cursor_agent_factory(review_chunks=(), flush_chunks=("late",))
        runs[0].cancel.side_effect = UnsupportedRunOperationError("cancel", "already terminal")
        monkeypatch.setattr(
            "code_review.review_backends.cursor.create_agent", AsyncMock(return_value=agent)
        )

        async def run() -> list[str]:
            async with cursor.review_session(pull_request_factory(), review_inputs_factory()) as session:
                return await collect(session["flush_text"]())

        assert asyncio.run(run()) == ["late"]

    def test_flush_retries_while_the_agent_is_still_busy(
        self, monkeypatch, mock_config, cursor_agent_factory, pull_request_factory, review_inputs_factory
    ) -> None:
        """Test that the flush send waits out the window where the cancelled run still holds the agent."""

        mock_config(cursor_api_key="key")
        monkeypatch.setattr("code_review.review_backends.cursor.FLUSH_SEND_RETRY_DELAY", timedelta(0))
        agent, runs = cursor_agent_factory(review_chunks=(), flush_chunks=("late",), busy_sends=2)
        monkeypatch.setattr(
            "code_review.review_backends.cursor.create_agent", AsyncMock(return_value=agent)
        )

        async def run() -> list[str]:
            async with cursor.review_session(pull_request_factory(), review_inputs_factory()) as session:
                return await collect(session["flush_text"]())

        assert asyncio.run(run()) == ["late"]
        assert agent.send.await_count == 4

    def test_agent_closes_even_when_the_flush_raises(
        self, monkeypatch, mock_config, cursor_agent_factory, pull_request_factory, review_inputs_factory
    ) -> None:
        """Test that the session agent is closed even when the flush turn fails."""

        mock_config(cursor_api_key="key")
        agent, runs = cursor_agent_factory(review_chunks=())
        runs[0].cancel.side_effect = AgentBusyError("stuck")
        monkeypatch.setattr(
            "code_review.review_backends.cursor.create_agent", AsyncMock(return_value=agent)
        )

        async def run() -> None:
            async with cursor.review_session(pull_request_factory(), review_inputs_factory()) as session:
                await collect(session["flush_text"]())

        with pytest.raises(AgentBusyError):
            asyncio.run(run())

        agent.close.assert_awaited_once()


class TestBridgeClientTimeout:
    """Test that the bridge read timeout tracks the configured review budget."""

    def test_caps_read_timeout_past_review_budget(self, mock_config) -> None:
        """Test that the bridge read timeout sits one minute past the review budget."""

        mock_config(review_timeout_minutes=15)
        timeout = cursor.bridge_client_timeout()

        assert timeout is not None
        assert timeout.read == pytest.approx(timedelta(minutes=16).total_seconds())
        assert timeout.connect == pytest.approx(timedelta(seconds=30).total_seconds())

    def test_disabled_review_timeout_removes_read_cap(self, mock_config) -> None:
        """Test that disabling the review timeout leaves the bridge without a read cap."""

        mock_config(review_timeout_minutes=None)

        assert cursor.bridge_client_timeout() is None

    def test_launch_passes_computed_client_timeout(self, monkeypatch, mock_config) -> None:
        """Test that the bridge launch receives the computed client timeout."""

        mock_config(review_timeout_minutes=15)
        recorded: dict[str, object] = {}

        async def _launch(**kwargs: object) -> object:
            recorded.update(kwargs)

            return object()

        monkeypatch.setattr(cursor.AsyncClient, "launch_bridge", _launch)
        asyncio.run(cursor.launch_bridge_with_retry())

        assert recorded["client_timeout"].read == pytest.approx(timedelta(minutes=16).total_seconds())
