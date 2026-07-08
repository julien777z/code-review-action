import asyncio
from collections.abc import AsyncIterator

import pytest

from code_review.config import CONFIG
from code_review.review_backends import cursor


async def chunk_stream(*parts: str) -> AsyncIterator[str]:
    """Yield the given text parts as an async chunk stream."""

    for part in parts:
        yield part


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


class TestRunCursorReview:
    """Test that the review turn loads the project rules only when enforcement is on."""

    @pytest.mark.parametrize("enforce", [True, False], ids=["enforcing", "not-enforcing"])
    def test_review_loads_rules_per_enforcement(
        self, monkeypatch, mock_config, pull_request_factory, review_github_mocks, enforce: bool
    ) -> None:
        """Test that run_agent is asked to load project rules to match the enforcement setting."""

        mock_config(cursor_api_key="key", enforce_project_rules=enforce)
        recorded: dict[str, bool] = {}

        def _run_agent(prompt: str, *, load_project_rules: bool = False) -> AsyncIterator[str]:
            recorded["load_project_rules"] = load_project_rules

            return chunk_stream(CONFIG["no_findings_marker"])

        monkeypatch.setattr("code_review.review_backends.cursor.run_agent", _run_agent)

        result = asyncio.run(cursor.run_cursor_review(pull_request_factory()))

        assert result.exit_code == 0
        assert recorded["load_project_rules"] is enforce
