import asyncio
from collections.abc import AsyncIterator

import pytest

from code_review.config import CONFIG
from code_review.errors import ReviewBackendError
from code_review.prompt import flush_prompt
from code_review.review_backends import claude


async def collect(chunks: AsyncIterator[str]) -> list[str]:
    """Drain an async chunk stream into a list."""

    return [chunk async for chunk in chunks]


async def open_and_collect(pr, inputs, *, flush: bool = False, both: bool = False) -> list[str]:
    """Open a Claude review session and drain its review turn, flush turn, or both in order."""

    async with claude.review_session(pr, inputs) as session:
        chunks: list[str] = []
        if both or not flush:
            chunks.extend(await collect(session["review_text"]()))

        if both or flush:
            chunks.extend(await collect(session["flush_text"]()))

        return chunks


class TestReviewSession:
    """Test that the Managed Agents session streams turns, mounts the repo on request, and tears down."""

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

        chunks = asyncio.run(open_and_collect(pull_request_factory(), review_inputs_factory()))

        assert chunks == [CONFIG["no_findings_marker"]]
        assert bool(client.beta.sessions.create.await_args.kwargs["resources"]) is repo_mounted

    def test_streams_message_text_until_idle(
        self,
        monkeypatch,
        mock_config,
        pull_request_factory,
        review_inputs_factory,
        managed_agent_client_factory,
        managed_agent_event_factory,
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

        chunks = asyncio.run(open_and_collect(pull_request_factory(), review_inputs_factory()))

        assert chunks == ["finding one\n", "finding two"]

    def test_mounts_repo_at_head_and_sends_prompt(
        self,
        monkeypatch,
        mock_config,
        pull_request_factory,
        review_inputs_factory,
        managed_agent_client_factory,
        managed_agent_event_factory,
    ) -> None:
        """Test that the PR repo is mounted at the head commit and the diff turn is sent as the user message."""

        mock_config(anthropic_api_key="key")
        events = [
            managed_agent_event_factory("agent.message", text="x"),
            managed_agent_event_factory("session.status_terminated"),
        ]
        client = managed_agent_client_factory(events)
        monkeypatch.setattr("code_review.review_backends.claude.anthropic.AsyncAnthropic", lambda **kwargs: client)

        pr = pull_request_factory(repo="octo/repo", head_sha="deadbeef")
        asyncio.run(open_and_collect(pr, review_inputs_factory(pr=pr, diff="DIFF_BODY")))

        resource = client.beta.sessions.create.await_args.kwargs["resources"][0]

        assert resource["url"] == "https://github.com/octo/repo"
        assert resource["checkout"]["sha"] == "deadbeef"
        sent = client.beta.sessions.events.send.await_args.kwargs["events"][0]

        assert "DIFF_BODY" in sent["content"][0]["text"]

    def test_flush_turn_reuses_the_session_with_a_fresh_stream(
        self,
        monkeypatch,
        mock_config,
        pull_request_factory,
        review_inputs_factory,
        managed_agent_client_factory,
        managed_agent_event_factory,
    ) -> None:
        """Test that the flush turn sends the wrap-up prompt into the same session over its own stream."""

        mock_config(anthropic_api_key="key")
        review_events = [
            managed_agent_event_factory("agent.message", text="partial"),
            managed_agent_event_factory("session.status_idle", stop_reason="end_turn"),
        ]
        flush_events = [
            managed_agent_event_factory("agent.message", text="late"),
            managed_agent_event_factory("session.status_idle", stop_reason="end_turn"),
        ]
        client = managed_agent_client_factory(review_events, flush_events)
        monkeypatch.setattr("code_review.review_backends.claude.anthropic.AsyncAnthropic", lambda **kwargs: client)

        chunks = asyncio.run(open_and_collect(pull_request_factory(), review_inputs_factory(), both=True))

        assert chunks == ["partial", "late"]
        assert client.beta.sessions.events.stream.await_count == 2
        session_ids = {call.kwargs["session_id"] for call in client.beta.sessions.events.send.await_args_list}

        assert session_ids == {"session-1"}
        flush_sent = client.beta.sessions.events.send.await_args_list[1].kwargs["events"][0]

        assert flush_sent["content"][0]["text"] == flush_prompt()
        client.beta.sessions.delete.assert_awaited_once()

    def test_flush_turn_skips_an_idle_before_its_own_text(
        self,
        monkeypatch,
        mock_config,
        pull_request_factory,
        review_inputs_factory,
        managed_agent_client_factory,
        managed_agent_event_factory,
    ) -> None:
        """Test that an idle left over from the interrupted turn does not end the flush before its reply."""

        mock_config(anthropic_api_key="key")
        flush_events = [
            managed_agent_event_factory("session.status_idle", stop_reason="end_turn"),
            managed_agent_event_factory("agent.message", text="late"),
            managed_agent_event_factory("session.status_idle", stop_reason="end_turn"),
        ]
        client = managed_agent_client_factory(flush_events)
        monkeypatch.setattr("code_review.review_backends.claude.anthropic.AsyncAnthropic", lambda **kwargs: client)

        chunks = asyncio.run(open_and_collect(pull_request_factory(), review_inputs_factory(), flush=True))

        assert chunks == ["late"]

    def test_no_output_streams_empty(
        self,
        monkeypatch,
        mock_config,
        pull_request_factory,
        review_inputs_factory,
        managed_agent_client_factory,
        managed_agent_event_factory,
    ) -> None:
        """Test that a session that answers with no text streams nothing for the runner's no-output check."""

        mock_config(anthropic_api_key="key")
        events = [managed_agent_event_factory("session.status_terminated")]
        client = managed_agent_client_factory(events)
        monkeypatch.setattr("code_review.review_backends.claude.anthropic.AsyncAnthropic", lambda **kwargs: client)

        chunks = asyncio.run(open_and_collect(pull_request_factory(), review_inputs_factory()))

        assert chunks == []

    def test_creates_and_tears_down_the_run_resources(
        self,
        monkeypatch,
        mock_config,
        pull_request_factory,
        review_inputs_factory,
        managed_agent_client_factory,
        managed_agent_event_factory,
    ) -> None:
        """Test that a fresh environment is created for the run and every resource is torn down once."""

        mock_config(anthropic_api_key="key")
        events = [
            managed_agent_event_factory("agent.message", text="x"),
            managed_agent_event_factory("session.status_idle", stop_reason="end_turn"),
        ]
        client = managed_agent_client_factory(events)
        monkeypatch.setattr("code_review.review_backends.claude.anthropic.AsyncAnthropic", lambda **kwargs: client)

        asyncio.run(open_and_collect(pull_request_factory(), review_inputs_factory()))

        client.beta.environments.create.assert_awaited_once()
        client.beta.sessions.delete.assert_awaited_once()
        client.beta.agents.archive.assert_awaited_once()
        client.beta.environments.delete.assert_awaited_once()

    def test_setup_failure_maps_to_review_failure_and_tears_down(
        self, monkeypatch, mock_config, pull_request_factory, review_inputs_factory, managed_agent_client_factory
    ) -> None:
        """Test that a failure creating the agent maps to a terminal review failure and still deletes the environment."""

        mock_config(anthropic_api_key="key")
        client = managed_agent_client_factory([])
        client.beta.agents.create.side_effect = RuntimeError("boom")
        monkeypatch.setattr("code_review.review_backends.claude.anthropic.AsyncAnthropic", lambda **kwargs: client)

        with pytest.raises(ReviewBackendError) as raised:
            asyncio.run(open_and_collect(pull_request_factory(), review_inputs_factory()))

        assert raised.value.retryable is False
        assert "Claude review failed: boom" in str(raised.value)
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
