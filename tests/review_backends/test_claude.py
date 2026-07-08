import asyncio
from collections.abc import AsyncIterator

import pytest

from code_review.config import CONFIG
from code_review.errors import ReviewBackendError
from code_review.review_backends import claude


async def collect(chunks: AsyncIterator[str]) -> list[str]:
    """Drain an async chunk stream into a list."""

    return [chunk async for chunk in chunks]


class TestReviewText:
    """Test that the review text stream mounts the repo per enforcement."""

    @pytest.mark.parametrize(
        ("enforce", "nearby", "repo_mounted"),
        [(True, False, True), (False, True, True), (False, False, False)],
        ids=["enforcing", "nearby-code", "neither"],
    )
    def test_repo_mount_follows_enforcement(
        self,
        monkeypatch,
        mock_config,
        pull_request_factory,
        review_inputs_factory,
        managed_agent_client_factory,
        managed_agent_event_factory,
        enforce: bool,
        nearby: bool,
        repo_mounted: bool,
    ) -> None:
        """Test that the session mounts the repo when rules are enforced or nearby code is weighed."""

        mock_config(anthropic_api_key="key", enforce_project_rules=enforce, simplify_nearby_code=nearby)
        events = [
            managed_agent_event_factory("agent.message", text=CONFIG["no_findings_marker"]),
            managed_agent_event_factory("session.status_idle", stop_reason="end_turn"),
        ]
        client = managed_agent_client_factory(events)
        monkeypatch.setattr("code_review.review_backends.claude.anthropic.AsyncAnthropic", lambda **kwargs: client)

        chunks = asyncio.run(collect(claude.review_text(pull_request_factory(), review_inputs_factory())))

        assert chunks == [CONFIG["no_findings_marker"]]
        assert bool(client.beta.sessions.create.await_args.kwargs["resources"]) is repo_mounted

    def test_managed_agent_runtime_error_maps_to_review_failure(
        self, monkeypatch, pull_request_factory, review_inputs_factory
    ) -> None:
        """Test that Managed Agents runtime setup failures use the shared review failure path."""

        async def fail_managed_agent_text(*args, **kwargs) -> AsyncIterator[str]:
            raise RuntimeError("agent setup failed")
            yield ""

        monkeypatch.setattr(claude, "managed_agent_text", fail_managed_agent_text)

        with pytest.raises(ReviewBackendError) as raised:
            asyncio.run(collect(claude.review_text(pull_request_factory(), review_inputs_factory())))

        assert raised.value.retryable is False
        assert "Claude review failed: agent setup failed" in str(raised.value)


class TestManagedAgentText:
    """Test that the Managed Agents session streams the agent's text and mounts the repo on request."""

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

        chunks = asyncio.run(collect(claude.managed_agent_text(pull_request_factory(), "review this", mount_repo=True)))

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
        asyncio.run(collect(claude.managed_agent_text(pr, "review this", mount_repo=True)))

        resource = client.beta.sessions.create.await_args.kwargs["resources"][0]

        assert resource["url"] == "https://github.com/octo/repo"
        assert resource["checkout"]["sha"] == "deadbeef"
        sent = client.beta.sessions.events.send.await_args.kwargs["events"][0]

        assert sent["content"][0]["text"] == "review this"

    def test_no_repo_mount_leaves_resources_empty(
        self, monkeypatch, mock_config, pull_request_factory, managed_agent_client_factory, managed_agent_event_factory
    ) -> None:
        """Test that skipping the repo mount creates a session with no resources."""

        mock_config(anthropic_api_key="key")
        events = [managed_agent_event_factory("agent.message", text="x"), managed_agent_event_factory("session.status_terminated")]
        client = managed_agent_client_factory(events)
        monkeypatch.setattr("code_review.review_backends.claude.anthropic.AsyncAnthropic", lambda **kwargs: client)

        asyncio.run(collect(claude.managed_agent_text(pull_request_factory(), "review this", mount_repo=False)))

        assert client.beta.sessions.create.await_args.kwargs["resources"] == []

    def test_no_output_raises(
        self, monkeypatch, mock_config, pull_request_factory, managed_agent_client_factory, managed_agent_event_factory
    ) -> None:
        """Test that a session that answers with no text fails loudly instead of reading as a clean review."""

        mock_config(anthropic_api_key="key")
        events = [managed_agent_event_factory("session.status_terminated")]
        client = managed_agent_client_factory(events)
        monkeypatch.setattr("code_review.review_backends.claude.anthropic.AsyncAnthropic", lambda **kwargs: client)

        chunks = asyncio.run(collect(claude.managed_agent_text(pull_request_factory(), "review this", mount_repo=True)))

        assert chunks == []

    def test_creates_and_tears_down_the_run_resources(
        self, monkeypatch, mock_config, pull_request_factory, managed_agent_client_factory, managed_agent_event_factory
    ) -> None:
        """Test that a fresh environment is created for the run and every resource is torn down."""

        mock_config(anthropic_api_key="key")
        events = [managed_agent_event_factory("agent.message", text="x"), managed_agent_event_factory("session.status_idle", stop_reason="end_turn")]
        client = managed_agent_client_factory(events)
        monkeypatch.setattr("code_review.review_backends.claude.anthropic.AsyncAnthropic", lambda **kwargs: client)

        asyncio.run(collect(claude.managed_agent_text(pull_request_factory(), "review this", mount_repo=True)))

        client.beta.environments.create.assert_awaited_once()
        client.beta.sessions.delete.assert_awaited_once()
        client.beta.agents.archive.assert_awaited_once()
        client.beta.environments.delete.assert_awaited_once()

    def test_tears_down_environment_when_agent_creation_fails(
        self, monkeypatch, mock_config, pull_request_factory, managed_agent_client_factory
    ) -> None:
        """Test that a failure creating the agent still deletes the environment and skips the missing resources."""

        mock_config(anthropic_api_key="key")
        client = managed_agent_client_factory([])
        client.beta.agents.create.side_effect = RuntimeError("boom")
        monkeypatch.setattr("code_review.review_backends.claude.anthropic.AsyncAnthropic", lambda **kwargs: client)

        with pytest.raises(RuntimeError, match="boom"):
            asyncio.run(collect(claude.managed_agent_text(pull_request_factory(), "review this", mount_repo=True)))

        client.beta.environments.delete.assert_awaited_once()
        client.beta.sessions.delete.assert_not_awaited()
        client.beta.agents.archive.assert_not_awaited()


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
