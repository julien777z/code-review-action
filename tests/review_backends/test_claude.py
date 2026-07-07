import asyncio

from code_review.review_backends import claude


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
