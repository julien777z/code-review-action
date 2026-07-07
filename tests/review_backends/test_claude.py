import asyncio
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest

from code_review import review
from code_review.review_backends import claude


async def collect(chunks: AsyncIterator[str]) -> list[str]:
    """Drain an async chunk stream into a list."""

    return [chunk async for chunk in chunks]


class TestRunClaudeReview:
    """Test that the review path is chosen by whether project rules are enforced."""

    def test_enforcing_uses_managed_agent(self, monkeypatch, mock_config, pull_request_factory) -> None:
        """Test that enforcing project rules reviews through a Managed Agents session."""

        mock_config(anthropic_api_key="key", enforce_project_rules=True)
        managed = AsyncMock(return_value=0)
        stream = AsyncMock(return_value=0)
        monkeypatch.setattr("code_review.review_backends.claude.run_managed_agent_review", managed)
        monkeypatch.setattr("code_review.review_backends.claude.run_messages_stream_review", stream)

        asyncio.run(claude.run_claude_review(pull_request_factory()))

        managed.assert_awaited_once()
        stream.assert_not_awaited()

    def test_not_enforcing_uses_messages_stream(self, monkeypatch, mock_config, pull_request_factory) -> None:
        """Test that skipping rule enforcement reviews through the Messages API stream."""

        mock_config(anthropic_api_key="key", enforce_project_rules=False)
        managed = AsyncMock(return_value=0)
        stream = AsyncMock(return_value=0)
        monkeypatch.setattr("code_review.review_backends.claude.run_managed_agent_review", managed)
        monkeypatch.setattr("code_review.review_backends.claude.run_messages_stream_review", stream)

        asyncio.run(claude.run_claude_review(pull_request_factory()))

        stream.assert_awaited_once()
        managed.assert_not_awaited()


class TestManagedAgentText:
    """Test that the Managed Agents session mounts the repo and streams the agent's text output."""

    def test_streams_message_text_until_idle(
        self, monkeypatch, mock_config, pull_request_factory, managed_agent_client_factory, managed_agent_event_factory
    ) -> None:
        """Test that text blocks are yielded and the stream stops on a terminal idle."""

        mock_config(anthropic_api_key="key")
        events = [
            managed_agent_event_factory("agent.message", text="finding one\n"),
            managed_agent_event_factory("agent.message", text="finding two"),
            managed_agent_event_factory("session.status_idle", stop_reason="end_turn"),
            managed_agent_event_factory("agent.message", text="after idle"),
        ]
        client = managed_agent_client_factory(events)
        monkeypatch.setattr("code_review.review_backends.claude.anthropic.AsyncAnthropic", lambda **kwargs: client)

        chunks = asyncio.run(collect(claude.managed_agent_text(pull_request_factory(), "review this")))

        assert chunks == ["finding one\n", "finding two"]

    def test_mounts_repo_at_head_and_sends_prompt(
        self, monkeypatch, mock_config, pull_request_factory, managed_agent_client_factory, managed_agent_event_factory
    ) -> None:
        """Test that the PR repo is mounted at the head commit and the prompt is sent as the user message."""

        mock_config(anthropic_api_key="key")
        events = [managed_agent_event_factory("agent.message", text="x"), managed_agent_event_factory("session.status_terminated")]
        client = managed_agent_client_factory(events)
        monkeypatch.setattr("code_review.review_backends.claude.anthropic.AsyncAnthropic", lambda **kwargs: client)

        pr = pull_request_factory(repo="octo/repo", head_sha="deadbeef")
        asyncio.run(collect(claude.managed_agent_text(pr, "review this")))

        resource = client.beta.sessions.create.await_args.kwargs["resources"][0]

        assert resource["url"] == "https://github.com/octo/repo"
        assert resource["checkout"]["sha"] == "deadbeef"
        sent = client.beta.sessions.events.send.await_args.kwargs["events"][0]

        assert sent["content"][0]["text"] == "review this"

    def test_no_output_raises(
        self, monkeypatch, mock_config, pull_request_factory, managed_agent_client_factory, managed_agent_event_factory
    ) -> None:
        """Test that a session that answers with no text fails loudly instead of reading as a clean review."""

        mock_config(anthropic_api_key="key")
        events = [managed_agent_event_factory("session.status_terminated")]
        client = managed_agent_client_factory(events)
        monkeypatch.setattr("code_review.review_backends.claude.anthropic.AsyncAnthropic", lambda **kwargs: client)

        with pytest.raises(review.ReviewBackendError):
            asyncio.run(collect(claude.managed_agent_text(pull_request_factory(), "review this")))

    def test_tears_down_session_and_created_environment(
        self, monkeypatch, mock_config, pull_request_factory, managed_agent_client_factory, managed_agent_event_factory
    ) -> None:
        """Test that the session and agent are removed and a run-created environment is deleted."""

        mock_config(anthropic_api_key="key", claude_environment_id="")
        events = [managed_agent_event_factory("agent.message", text="x"), managed_agent_event_factory("session.status_idle", stop_reason="end_turn")]
        client = managed_agent_client_factory(events)
        monkeypatch.setattr("code_review.review_backends.claude.anthropic.AsyncAnthropic", lambda **kwargs: client)

        asyncio.run(collect(claude.managed_agent_text(pull_request_factory(), "review this")))

        client.beta.sessions.delete.assert_awaited_once()
        client.beta.agents.archive.assert_awaited_once()
        client.beta.environments.create.assert_awaited_once()
        client.beta.environments.delete.assert_awaited_once()

    def test_reuses_configured_environment(
        self, monkeypatch, mock_config, pull_request_factory, managed_agent_client_factory, managed_agent_event_factory
    ) -> None:
        """Test that a configured environment is reused and never created or deleted."""

        mock_config(anthropic_api_key="key", claude_environment_id="env-configured")
        events = [managed_agent_event_factory("agent.message", text="x"), managed_agent_event_factory("session.status_idle", stop_reason="end_turn")]
        client = managed_agent_client_factory(events)
        monkeypatch.setattr("code_review.review_backends.claude.anthropic.AsyncAnthropic", lambda **kwargs: client)

        asyncio.run(collect(claude.managed_agent_text(pull_request_factory(), "review this")))

        assert client.beta.sessions.create.await_args.kwargs["environment_id"] == "env-configured"
        client.beta.environments.create.assert_not_awaited()
        client.beta.environments.delete.assert_not_awaited()


class TestGenerateText:
    """Test that the single-shot Claude completion returns the joined text output."""

    def test_joins_text_blocks(self, monkeypatch, mock_config, anthropic_client_factory) -> None:
        """Test that the text content of the response is returned."""

        mock_config(anthropic_api_key="key")
        client = anthropic_client_factory(text="Generated summary")
        monkeypatch.setattr("code_review.review_backends.claude.anthropic.AsyncAnthropic", lambda **kwargs: client)

        assert asyncio.run(claude.generate_text("prompt")) == "Generated summary"

    def test_sends_prompt_to_the_model(self, monkeypatch, mock_config, anthropic_client_factory) -> None:
        """Test that the prompt is forwarded as the user message."""

        mock_config(anthropic_api_key="key")
        client = anthropic_client_factory()
        monkeypatch.setattr("code_review.review_backends.claude.anthropic.AsyncAnthropic", lambda **kwargs: client)

        asyncio.run(claude.generate_text("Summarize this"))
        kwargs = client.messages.create.await_args.kwargs

        assert kwargs["messages"] == [{"role": "user", "content": "Summarize this"}]
