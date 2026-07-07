import logging
from collections.abc import AsyncIterator
from typing import Final

import anthropic

from code_review import review
from code_review.config import CONFIG, SETTINGS
from code_review.models.shared.findings import Finding
from code_review.models.shared.pull_request import PullRequestContext, ReviewInputs
from code_review.prompt import pull_request_message, review_instructions
from code_review.review_backends.jsonl import iter_findings

logger = logging.getLogger("code_review.claude")

CLAUDE_MAX_TOKENS: Final[int] = 16000
SUMMARY_MAX_TOKENS: Final[int] = 1500


async def run_claude_api_review(pr: PullRequestContext) -> int:
    """Review the PR with the Claude Messages API, streaming each finding as the model emits it."""

    async def _findings(inputs: ReviewInputs) -> AsyncIterator[Finding]:
        try:
            async with anthropic.AsyncAnthropic(api_key=SETTINGS.anthropic_api_key) as client:
                async with client.messages.stream(
                    model=SETTINGS.claude_model,
                    max_tokens=CLAUDE_MAX_TOKENS,
                    thinking={"type": "adaptive"},
                    system=[
                        {
                            "type": "text",
                            "text": review_instructions(),
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    messages=[{"role": "user", "content": pull_request_message(inputs)}],
                ) as stream:
                    async for finding in iter_findings(stream.text_stream):
                        yield finding
        except anthropic.APIError as exc:
            retryable = isinstance(
                exc,
                (
                    anthropic.APIConnectionError,
                    anthropic.InternalServerError,
                    anthropic.OverloadedError,
                    anthropic.RateLimitError,
                ),
            )

            raise review.ReviewBackendError(f"Claude review request failed: {exc}", retryable=retryable) from exc

    return await review.run_review_round(pr, CONFIG["review_marker"], _findings)


async def generate_text(prompt: str) -> str:
    """Run a single-shot Claude completion and return the joined text output."""

    async with anthropic.AsyncAnthropic(api_key=SETTINGS.anthropic_api_key) as client:
        message = await client.messages.create(
            model=SETTINGS.claude_model,
            max_tokens=SUMMARY_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )

    return "".join(block.text for block in message.content if block.type == "text")
