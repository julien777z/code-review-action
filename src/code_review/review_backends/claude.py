import logging
from collections.abc import AsyncIterator
from typing import Final

import anthropic
from anthropic.types.beta import (
    BetaCloudConfigParams,
    BetaManagedAgentsCommitCheckoutParam,
    BetaManagedAgentsGitHubRepositoryResourceParams,
    BetaUnrestrictedNetworkParam,
)

from code_review import review
from code_review.config import CONFIG, SETTINGS
from code_review.models.shared.findings import Finding
from code_review.models.shared.pull_request import PullRequestContext, ReviewInputs
from code_review.prompt import pull_request_message, review_instructions
from code_review.review_backends.jsonl import iter_findings

logger = logging.getLogger("code_review.claude")

CLAUDE_MAX_TOKENS: Final[int] = 16000
SUMMARY_MAX_TOKENS: Final[int] = 1500

MANAGED_AGENTS_BETA: Final[str] = "managed-agents-2026-04-01"
REVIEW_AGENT_NAME: Final[str] = "code-review-action"


def is_retryable_api_error(exc: anthropic.APIError) -> bool:
    """Return whether an Anthropic API error is worth retrying."""

    return isinstance(
        exc,
        (
            anthropic.APIConnectionError,
            anthropic.InternalServerError,
            anthropic.OverloadedError,
            anthropic.RateLimitError,
        ),
    )


def github_repository_resource(pr: PullRequestContext) -> BetaManagedAgentsGitHubRepositoryResourceParams:
    """Describe the PR repository mount so the agent clones it and loads the project's rules."""

    return BetaManagedAgentsGitHubRepositoryResourceParams(
        type="github_repository",
        url=f"https://github.com/{pr.repo}",
        authorization_token=SETTINGS.github_token,
        checkout=BetaManagedAgentsCommitCheckoutParam(type="commit", sha=pr.head_sha),
    )


async def resolve_environment(client: anthropic.AsyncAnthropic) -> tuple[str, bool]:
    """Return the environment to run the session in and whether this run created it."""

    if SETTINGS.claude_environment_id:
        return SETTINGS.claude_environment_id, False

    environment = await client.beta.environments.create(
        name=REVIEW_AGENT_NAME,
        config=BetaCloudConfigParams(type="cloud", networking=BetaUnrestrictedNetworkParam(type="unrestricted")),
        betas=[MANAGED_AGENTS_BETA],
    )

    return environment.id, True


async def teardown_managed_agent(
    client: anthropic.AsyncAnthropic,
    session_id: str,
    agent_id: str,
    environment_id: str,
    created_environment: bool,
) -> None:
    """Delete the session and agent, and the environment when this run created it, tolerating teardown failures."""

    try:
        await client.beta.sessions.delete(session_id, betas=[MANAGED_AGENTS_BETA])
        await client.beta.agents.archive(agent_id, betas=[MANAGED_AGENTS_BETA])
        if created_environment:
            await client.beta.environments.delete(environment_id, betas=[MANAGED_AGENTS_BETA])
    except anthropic.APIError as exc:
        logger.warning("Could not tear down the Claude agent session: %s", exc)


async def managed_agent_text(pr: PullRequestContext, user_message: str) -> AsyncIterator[str]:
    """Run one Managed Agents turn with the PR repo mounted, streaming the agent's response text."""

    async with anthropic.AsyncAnthropic(api_key=SETTINGS.anthropic_api_key) as client:
        environment_id, created_environment = await resolve_environment(client)
        agent = await client.beta.agents.create(
            name=REVIEW_AGENT_NAME,
            model=SETTINGS.claude_model,
            system=review_instructions(),
            tools=[{"type": "agent_toolset_20260401", "default_config": {"enabled": True}}],
            betas=[MANAGED_AGENTS_BETA],
        )
        session = await client.beta.sessions.create(
            agent={"type": "agent", "id": agent.id, "version": agent.version},
            environment_id=environment_id,
            resources=[github_repository_resource(pr)],
            betas=[MANAGED_AGENTS_BETA],
        )

        try:
            produced_text = False
            async with await client.beta.sessions.events.stream(
                session_id=session.id, betas=[MANAGED_AGENTS_BETA]
            ) as stream:
                await client.beta.sessions.events.send(
                    session_id=session.id,
                    events=[{"type": "user.message", "content": [{"type": "text", "text": user_message}]}],
                    betas=[MANAGED_AGENTS_BETA],
                )
                async for event in stream:
                    if event.type == "agent.message":
                        for block in event.content:
                            if block.type == "text":
                                produced_text = True

                                yield block.text
                    elif event.type == "session.status_idle":
                        if event.stop_reason.type == "requires_action":
                            continue

                        break
                    elif event.type == "session.status_terminated":
                        break

            # No text at all means the session errored or terminated without answering; fail loudly rather
            # than let an empty result read as a clean, no-findings review that would approve the PR.
            if not produced_text:
                raise review.ReviewBackendError("The Claude agent session produced no output.", retryable=True)
        finally:
            await teardown_managed_agent(client, session.id, agent.id, environment_id, created_environment)


async def run_managed_agent_review(pr: PullRequestContext) -> int:
    """Review the PR with a Managed Agents session that mounts the repo so Claude loads the project rules."""

    async def _findings(inputs: ReviewInputs) -> AsyncIterator[Finding]:
        try:
            async for finding in iter_findings(managed_agent_text(pr, pull_request_message(inputs))):
                yield finding
        except anthropic.APIError as exc:
            raise review.ReviewBackendError(
                f"Claude agent review failed: {exc}", retryable=is_retryable_api_error(exc)
            ) from exc

    return await review.run_review_round(pr, CONFIG["review_marker"], _findings)


async def run_messages_stream_review(pr: PullRequestContext) -> int:
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
            raise review.ReviewBackendError(
                f"Claude review request failed: {exc}", retryable=is_retryable_api_error(exc)
            ) from exc

    return await review.run_review_round(pr, CONFIG["review_marker"], _findings)


async def run_claude_review(pr: PullRequestContext) -> int:
    """Review the PR with Claude, mounting the repo via Managed Agents when enforcing project rules."""

    if SETTINGS.enforce_project_rules:
        return await run_managed_agent_review(pr)

    return await run_messages_stream_review(pr)


async def generate_text(prompt: str) -> str:
    """Run a single-shot Claude completion and return the joined text output."""

    async with anthropic.AsyncAnthropic(api_key=SETTINGS.anthropic_api_key) as client:
        message = await client.messages.create(
            model=SETTINGS.claude_model,
            max_tokens=SUMMARY_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )

    return "".join(block.text for block in message.content if block.type == "text")
