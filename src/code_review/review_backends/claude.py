import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ClaudeSDKError
from claude_agent_sdk.types import AssistantMessage, RateLimitEvent, ResultMessage, StreamEvent, TextBlock

from code_review.config import SETTINGS
from code_review.errors import ReviewBackendError
from code_review.models.backend import ReviewSessionStreams
from code_review.models.pull_request import PullRequestContext, ReviewInputs
from code_review.prompt import flush_prompt, pull_request_message, review_instructions


def claude_options(*, reviewing: bool = True) -> ClaudeAgentOptions:
    """Build read-only Claude Code options for a review or standalone summary turn."""

    environment = dict(os.environ)
    environment["CLAUDE_CODE_OAUTH_TOKEN"] = SETTINGS.claude_code_oauth_token
    environment.pop("ANTHROPIC_API_KEY", None)
    environment.pop("CODEX_AUTH_JSON", None)

    return ClaudeAgentOptions(
        allowed_tools=["Read", "Glob", "Grep", "Agent", "WebFetch", "WebSearch"] if reviewing else [],
        disallowed_tools=["Bash", "Edit", "Write", "NotebookEdit"],
        system_prompt=review_instructions() if reviewing else None,
        model=SETTINGS.claude_model,
        cwd=os.environ.get("GITHUB_WORKSPACE") or os.getcwd(),
        env=environment,
        include_partial_messages=True,
        permission_mode="dontAsk",
        setting_sources=["project"] if reviewing else [],
    )


def stream_delta(message: StreamEvent) -> str | None:
    """Return a text delta from a Claude stream event when present."""

    event = message.event
    if event.get("type") != "content_block_delta":
        return None

    delta = event.get("delta")
    if not isinstance(delta, dict) or delta.get("type") != "text_delta":
        return None

    text = delta.get("text")

    return text if isinstance(text, str) else None


async def turn_text(client: ClaudeSDKClient, prompt: str, *, interrupt: bool = False) -> AsyncIterator[str]:
    """Run one Claude Code turn and stream only its assistant text."""

    if interrupt:
        await client.interrupt()
        async for _ in client.receive_response():
            pass

    await client.query(prompt)
    partial_output = False
    async for message in client.receive_response():
        if isinstance(message, RateLimitEvent) and message.rate_limit_info.status == "rejected":
            raise ReviewBackendError(
                "Claude subscription usage limit reached.", usage_limited=True
            )

        if isinstance(message, StreamEvent) and (delta := stream_delta(message)) is not None:
            partial_output = True
            yield delta
        elif isinstance(message, AssistantMessage) and not partial_output:
            for block in message.content:
                if isinstance(block, TextBlock):
                    yield block.text
        elif isinstance(message, ResultMessage) and message.is_error:
            details = "; ".join(message.errors or ()) or message.result or message.subtype
            usage_limited = any(
                marker in details.lower()
                for marker in ("session limit", "weekly limit", "opus limit", "usage limit")
            )
            raise ReviewBackendError(
                f"Claude Code failed: {details}", usage_limited=usage_limited
            )


@asynccontextmanager
async def review_session(pr: PullRequestContext, inputs: ReviewInputs) -> AsyncIterator[ReviewSessionStreams]:
    """Open a persistent Claude Code review session with interruptible review and flush turns."""

    try:
        async with ClaudeSDKClient(options=claude_options()) as client:
            yield ReviewSessionStreams(
                review_text=lambda: turn_text(client, pull_request_message(inputs)),
                flush_text=lambda: turn_text(client, flush_prompt(), interrupt=True),
            )
    except ClaudeSDKError as exc:
        raise ReviewBackendError(f"Claude Code failed: {exc}", retryable=True) from exc


async def generate_text(prompt: str) -> str:
    """Run one Claude Code turn and return its complete assistant text."""

    output: list[str] = []
    try:
        async with ClaudeSDKClient(options=claude_options(reviewing=False)) as client:
            async for chunk in turn_text(client, prompt):
                output.append(chunk)
    except ClaudeSDKError as exc:
        raise ReviewBackendError(f"Claude Code failed: {exc}", retryable=True) from exc

    return "".join(output)
