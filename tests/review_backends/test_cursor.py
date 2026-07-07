import asyncio
from collections.abc import AsyncIterator

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
