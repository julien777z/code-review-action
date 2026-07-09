import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest

from code_review.config import CONFIG
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

    def test_summary_turn_does_not_load_project_rules(self, monkeypatch, mock_config) -> None:
        """Test that the summary turn runs a local agent without loading the project settings."""

        mock_config(cursor_api_key="key")
        recorded: dict[str, bool] = {}

        def _run_agent(prompt: str, *, load_project_rules: bool = False) -> AsyncIterator[str]:
            recorded["load_project_rules"] = load_project_rules

            return chunk_stream("ok")

        monkeypatch.setattr("code_review.review_backends.cursor.run_agent", _run_agent)

        asyncio.run(cursor.generate_text("prompt"))

        assert recorded["load_project_rules"] is False


class TestReviewText:
    """Test that the review text stream loads project rules when repo context is needed."""

    @pytest.mark.parametrize(
        ("enforce", "nearby", "loads_rules"),
        [(True, False, True), (False, True, True), (False, False, False)],
        ids=["enforcing", "nearby-code", "neither"],
    )
    def test_review_loads_rules_per_enforcement(
        self,
        monkeypatch,
        mock_config,
        pull_request_factory,
        review_inputs_factory,
        enforce: bool,
        nearby: bool,
        loads_rules: bool,
    ) -> None:
        """Test that run_agent is asked to load project rules when review criteria need repo context."""

        mock_config(cursor_api_key="key", enforce_project_rules=enforce, simplify_nearby_code=nearby)
        recorded: dict[str, bool] = {}

        def _run_agent(prompt: str, *, load_project_rules: bool = False) -> AsyncIterator[str]:
            recorded["load_project_rules"] = load_project_rules

            return chunk_stream(CONFIG["no_findings_marker"])

        monkeypatch.setattr("code_review.review_backends.cursor.run_agent", _run_agent)

        chunks = asyncio.run(collect(cursor.review_text(pull_request_factory(), review_inputs_factory())))

        assert chunks == [CONFIG["no_findings_marker"]]
        assert recorded["load_project_rules"] is loads_rules


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
