import asyncio
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from claude_agent_sdk.types import RateLimitEvent, RateLimitInfo, ResultMessage, StreamEvent

from code_review.errors import ReviewBackendError
from code_review.review_backends import claude


async def collect(chunks: AsyncIterator[str]) -> list[str]:
    """Drain an async chunk stream into a list."""

    return [chunk async for chunk in chunks]


def stream_event(text: str) -> StreamEvent:
    """Build one Claude text-delta stream event."""

    return StreamEvent(
        uuid="message-1",
        session_id="session-1",
        event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
    )


def result_message(*, error: str | None = None) -> ResultMessage:
    """Build one terminal Claude result message."""

    return ResultMessage(
        subtype="error_during_execution" if error else "success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=error is not None,
        num_turns=1,
        session_id="session-1",
        result=error,
        errors=[error] if error else [],
    )


ClaudeMessage = RateLimitEvent | ResultMessage | StreamEvent


def claude_client(*responses: list[ClaudeMessage]) -> MagicMock:
    """Build a persistent ClaudeSDKClient double with one message stream per turn."""

    client = MagicMock()
    client.query = AsyncMock(return_value=None)
    client.interrupt = AsyncMock(return_value=None)
    streams = iter(responses)

    async def _receive() -> AsyncIterator[ClaudeMessage]:
        for message in next(streams):
            yield message

    client.receive_response = _receive
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    return client


class TestClaudeOptions:
    """Test that Claude Code runs with subscription auth and read-only tools."""

    def test_uses_oauth_and_disables_mutating_tools(self, mock_config, monkeypatch) -> None:
        """Test that Claude options carry OAuth while denying shell and write tools."""

        mock_config(claude_code_oauth_token="oauth-token")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "api-key")
        monkeypatch.setenv("CODEX_AUTH_JSON", "codex-secret")

        options = claude.claude_options()

        assert options.env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-token"
        assert "ANTHROPIC_API_KEY" not in options.env
        assert "CODEX_AUTH_JSON" not in options.env
        assert {"Bash", "Edit", "Write"}.issubset(options.disallowed_tools)


class TestReviewSession:
    """Test that Claude review turns stream, interrupt, and detect usage exhaustion."""

    def test_streams_review_then_interrupts_before_flush(
        self, monkeypatch, mock_config, pull_request_factory, review_inputs_factory
    ) -> None:
        """Test that the flush drains the interrupted result before sending its follow-up."""

        mock_config(claude_code_oauth_token="oauth-token")
        client = claude_client(
            [stream_event("review"), result_message()],
            [result_message(error="interrupted")],
            [stream_event("flush"), result_message()],
        )
        monkeypatch.setattr(claude, "ClaudeSDKClient", lambda options: client)

        async def run() -> tuple[list[str], list[str]]:
            async with claude.review_session(
                pull_request_factory(), review_inputs_factory()
            ) as session:
                review = await collect(session["review_text"]())
                flush = await collect(session["flush_text"]())

                return review, flush

        review, flush = asyncio.run(run())

        assert review == ["review"]
        assert flush == ["flush"]
        client.interrupt.assert_awaited_once()
        assert client.query.await_count == 2

    def test_rejected_rate_limit_is_usage_exhaustion(self, mock_config) -> None:
        """Test that a rejected subscription rate event enables provider fallback."""

        mock_config(claude_code_oauth_token="oauth-token")
        client = claude_client(
            [
                RateLimitEvent(
                    rate_limit_info=RateLimitInfo(status="rejected"),
                    uuid="limit-1",
                    session_id="session-1",
                )
            ]
        )

        with pytest.raises(ReviewBackendError) as raised:
            asyncio.run(collect(claude.turn_text(client, "review")))

        assert raised.value.usage_limited is True


class TestGenerateText:
    """Test that Claude summary generation joins streamed text."""

    def test_joins_text_deltas(self, monkeypatch, mock_config) -> None:
        """Test that a single summary turn returns its complete text."""

        mock_config(claude_code_oauth_token="oauth-token")
        client = claude_client([stream_event("Generated "), stream_event("summary"), result_message()])
        monkeypatch.setattr(claude, "ClaudeSDKClient", lambda options: client)

        assert asyncio.run(claude.generate_text("prompt")) == "Generated summary"
