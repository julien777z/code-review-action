import asyncio
from collections.abc import AsyncIterator

from cursor_sdk import CursorAgentError

from code_review.review_backends import cursor


async def chunk_stream(*parts: str) -> AsyncIterator[str]:
    """Yield the given text parts as an async chunk stream."""

    for part in parts:
        yield part


class TestCursorErrorMessage:
    """Test that a missing-repo-access failure produces an actionable message."""

    def test_missing_access_explains_how_to_fix(self) -> None:
        """Test that an SCM access failure explains how to grant access or disable enforcement."""

        exc = CursorAgentError("invalid_argument: The SCM integration does not have access to repository x")
        message = cursor.cursor_error_message(exc)

        assert "repository access" in message
        assert "enforce-project-rules" in message

    def test_other_failure_uses_generic_message(self) -> None:
        """Test that an unrelated failure keeps the generic message."""

        exc = CursorAgentError("something else broke")

        assert cursor.cursor_error_message(exc) == "Cursor agent run failed: something else broke"


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
