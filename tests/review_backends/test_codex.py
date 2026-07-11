import asyncio
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from code_review.errors import ReviewBackendError
from code_review.models.codex import CodexRpcMessage
from code_review.review_backends import codex


async def collect(chunks: AsyncIterator[str]) -> list[str]:
    """Drain an async chunk stream into a list."""

    return [chunk async for chunk in chunks]


class TestUsageLimitError:
    """Test structured Codex subscription-limit classification."""

    @pytest.mark.parametrize(
        ("info", "expected"),
        [
            ("UsageLimitExceeded", True),
            ({"type": "UsageLimitExceeded"}, True),
            ({"type": "Unauthorized"}, False),
            (None, False),
        ],
        ids=["string", "tagged", "other", "missing"],
    )
    def test_classifies_only_usage_limits(self, info, expected: bool) -> None:
        """Test that only the app-server usage-limit variant enables fallback."""

        error = None if info is None else {"codexErrorInfo": info}

        assert codex.usage_limit_error(error) is expected


class TestTurnText:
    """Test Codex turn streaming and failure handling."""

    def test_streams_deltas_with_terra_high(self, mock_config) -> None:
        """Test that turns select the configured model with high reasoning."""

        mock_config(codex_model="gpt-5.6-terra")
        client = codex.CodexAppServer(MagicMock())
        client.thread_id = "thread-1"
        client.request = AsyncMock(
            return_value={"turn": {"id": "turn-1", "status": "inProgress"}}
        )
        client.read = AsyncMock(
            side_effect=[
                CodexRpcMessage(
                    method="item/agentMessage/delta", params={"delta": "finding"}
                ),
                CodexRpcMessage(
                    method="turn/completed",
                    params={"turn": {"id": "turn-1", "status": "completed"}},
                ),
            ]
        )

        assert asyncio.run(collect(client.turn_text("review"))) == ["finding"]
        params = client.request.await_args.args[1]
        assert params["model"] == "gpt-5.6-terra"
        assert params["effort"] == "high"

    def test_usage_limit_failure_enables_fallback(self) -> None:
        """Test that a failed usage-limited turn raises the shared fallback signal."""

        client = codex.CodexAppServer(MagicMock())
        client.thread_id = "thread-1"
        client.request = AsyncMock(
            return_value={"turn": {"id": "turn-1", "status": "inProgress"}}
        )
        client.read = AsyncMock(
            return_value=CodexRpcMessage(
                method="turn/completed",
                params={
                    "turn": {
                        "id": "turn-1",
                        "status": "failed",
                        "error": {
                            "message": "usage reached",
                            "codexErrorInfo": "UsageLimitExceeded",
                        },
                    }
                },
            )
        )

        with pytest.raises(ReviewBackendError) as raised:
            asyncio.run(collect(client.turn_text("review")))

        assert raised.value.usage_limited is True

    def test_error_notification_raises(self) -> None:
        """Test that a non-limit error notification cannot leave the stream waiting forever."""

        client = codex.CodexAppServer(MagicMock())
        client.thread_id = "thread-1"
        client.request = AsyncMock(
            return_value={"turn": {"id": "turn-1", "status": "inProgress"}}
        )
        client.read = AsyncMock(
            return_value=CodexRpcMessage(
                method="error",
                params={"error": {"message": "transport failed"}},
            )
        )

        with pytest.raises(ReviewBackendError, match="transport failed"):
            asyncio.run(collect(client.turn_text("review")))
