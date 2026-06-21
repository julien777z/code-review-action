import asyncio
from collections.abc import Callable
from datetime import timedelta

import pytest
from cursor_sdk import APITimeoutError, AuthenticationError, CursorAgentError

from code_review.models.shared.severity import DiffSide, Severity
from code_review.review import ReviewBackendError
from code_review.review_backends import cursor
from code_review.review_backends.cursor import AGENT_RUN_ATTEMPTS, parse_cursor_reply, run_agent


@pytest.fixture
def flaky_run_agent_once(monkeypatch) -> Callable[..., list[int]]:
    """Patch run_agent_once to fail a set number of times before returning a reply."""

    monkeypatch.setattr(cursor, "AGENT_RETRY_BACKOFF", timedelta(0))

    def _build(*, failures: int, error: CursorAgentError, reply: str = "{}") -> list[int]:
        calls: list[int] = []

        async def _run_once(prompt: str) -> str:
            calls.append(len(calls))
            if len(calls) <= failures:
                raise error

            return reply

        monkeypatch.setattr(cursor, "run_agent_once", _run_once)

        return calls

    return _build


class TestParseCursorReply:
    """Test that the Cursor reply parses, normalizes, and rejects bad output."""

    def test_plain_object(self) -> None:
        """Test that a plain findings object parses with a normalized severity."""

        text = '{"findings":[{"path":"a.py","line":3,"side":"RIGHT","severity":"high","title":"T","body":"B"}]}'
        findings = parse_cursor_reply(text)

        assert len(findings) == 1
        assert findings[0].severity is Severity.HIGH

    def test_fenced_code_block(self) -> None:
        """Test that a fenced JSON block parses and the side normalizes."""

        text = '```json\n{"findings":[{"path":"a.py","line":3,"side":"LEFT","severity":"low","title":"T","body":"B"}]}\n```'

        assert parse_cursor_reply(text)[0].side is DiffSide.LEFT

    def test_list_root(self) -> None:
        """Test that a bare findings list parses."""

        text = '[{"path":"a.py","line":3,"side":"RIGHT","severity":"medium","title":"T","body":"B"}]'

        assert len(parse_cursor_reply(text)) == 1

    def test_capitalized_severity_normalized(self) -> None:
        """Test that a capitalized severity word is normalized to the enum."""

        text = '{"findings":[{"path":"a.py","line":1,"side":"RIGHT","severity":"Critical","title":"T","body":"B"}]}'

        assert parse_cursor_reply(text)[0].severity is Severity.CRITICAL

    def test_unknown_severity_skipped(self) -> None:
        """Test that a finding with an unknown severity is skipped rather than crashing."""

        text = '{"findings":[{"path":"a.py","line":1,"side":"RIGHT","severity":"bogus","title":"T","body":"B"}]}'

        assert parse_cursor_reply(text) == []

    def test_empty_findings(self) -> None:
        """Test that an empty findings list yields no findings."""

        assert parse_cursor_reply('{"findings":[]}') == []

    def test_bad_json_raises(self) -> None:
        """Test that unparseable output raises a backend error."""

        with pytest.raises(ReviewBackendError):
            parse_cursor_reply("not json at all")


class TestRunAgentRetry:
    """Test that run_agent retries transient bridge failures and surfaces fatal ones."""

    @pytest.mark.parametrize(
        "error",
        [
            APITimeoutError("Bridge request timed out: ReadTimeout", is_retryable=True),
            CursorAgentError("Bridge request failed: ConnectError", is_retryable=True),
        ],
        ids=["timeout", "network"],
    )
    def test_retries_then_succeeds(self, flaky_run_agent_once, error: CursorAgentError) -> None:
        """Test that a retryable failure is retried until the agent run succeeds."""

        calls = flaky_run_agent_once(failures=1, error=error, reply='{"findings":[]}')

        result = asyncio.run(run_agent("prompt"))

        assert result == '{"findings":[]}'
        assert len(calls) == 2

    def test_raises_after_exhausting_attempts(self, flaky_run_agent_once) -> None:
        """Test that a persistently timing-out run gives up after the attempt budget."""

        error = APITimeoutError("Bridge request timed out: ReadTimeout", is_retryable=True)
        calls = flaky_run_agent_once(failures=AGENT_RUN_ATTEMPTS, error=error)

        with pytest.raises(APITimeoutError):
            asyncio.run(run_agent("prompt"))

        assert len(calls) == AGENT_RUN_ATTEMPTS

    def test_non_retryable_error_not_retried(self, flaky_run_agent_once) -> None:
        """Test that a non-retryable error surfaces immediately without further attempts."""

        error = AuthenticationError("Invalid API key")
        calls = flaky_run_agent_once(failures=AGENT_RUN_ATTEMPTS, error=error)

        with pytest.raises(AuthenticationError):
            asyncio.run(run_agent("prompt"))

        assert len(calls) == 1
