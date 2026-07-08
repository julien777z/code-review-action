import logging
from collections.abc import AsyncIterator, Awaitable
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


async def create_environment(client: anthropic.AsyncAnthropic) -> str:
    """Create a fresh cloud environment for this run's session and return its id."""

    environment = await client.beta.environments.create(
        name=REVIEW_AGENT_NAME,
        config=BetaCloudConfigParams(type="cloud", networking=BetaUnrestrictedNetworkParam(type="unrestricted")),
        betas=[MANAGED_AGENTS_BETA],
    )

    return environment.id


async def teardown_managed_agent(
    client: anthropic.AsyncAnthropic, environment_id: str, agent_id: str | None, session_id: str | None
) -> None:
    """Delete whichever of the run's session, agent, and environment were created, tolerating failures."""

    async def _delete(action: Awaitable[object]) -> None:
        """Await one teardown call and swallow an API failure so the others still run."""

        try:
            await action
        except anthropic.APIError as exc:
            logger.warning("Could not tear down a Claude agent resource: %s", exc)

    if session_id is not None:
        await _delete(client.beta.sessions.delete(session_id, betas=[MANAGED_AGENTS_BETA]))

    if agent_id is not None:
        await _delete(client.beta.agents.archive(agent_id, betas=[MANAGED_AGENTS_BETA]))

    await _delete(client.beta.environments.delete(environment_id, betas=[MANAGED_AGENTS_BETA]))


async def managed_agent_text(pr: PullRequestContext, user_message: str, *, mount_repo: bool) -> AsyncIterator[str]:
    """Run one Managed Agents turn, streaming the agent's response text and mounting the repo when asked."""

    async with anthropic.AsyncAnthropic(api_key=SETTINGS.anthropic_api_key) as client:
        environment_id: str | None = None
        agent_id: str | None = None
        session_id: str | None = None

        try:
            environment_id = await create_environment(client)
            agent = await client.beta.agents.create(
                name=REVIEW_AGENT_NAME,
                model=SETTINGS.claude_model,
                system=review_instructions(),
                tools=[{"type": "agent_toolset_20260401", "default_config": {"enabled": True}}],
                betas=[MANAGED_AGENTS_BETA],
            )
            agent_id = agent.id
            session = await client.beta.sessions.create(
                agent={"type": "agent", "id": agent.id, "version": agent.version},
                environment_id=environment_id,
                resources=[github_repository_resource(pr)] if mount_repo else [],
                betas=[MANAGED_AGENTS_BETA],
            )
            session_id = session.id

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

            if not produced_text:
                raise review.ReviewBackendError("The Claude agent session produced no output.", retryable=True)
        except review.ReviewBackendError:
            raise
        except anthropic.APIError as exc:
            raise review.ReviewBackendError(
                f"Claude agent review failed: {exc}", retryable=is_retryable_api_error(exc)
            ) from exc
        except RuntimeError as exc:
            raise review.ReviewBackendError(f"Claude agent setup failed: {exc}", retryable=True) from exc
        finally:
            if environment_id is not None:
                await teardown_managed_agent(client, environment_id, agent_id, session_id)


async def run_claude_review(pr: PullRequestContext) -> review.ReviewRoundResult:
    """Review the PR with a Managed Agents session."""

    async def _findings(inputs: ReviewInputs) -> AsyncIterator[Finding]:
        mount_repo = SETTINGS.enforce_project_rules or SETTINGS.simplify_nearby_code
        stream = managed_agent_text(pr, pull_request_message(inputs), mount_repo=mount_repo)
        try:
            async for finding in iter_findings(stream):
                yield finding
        except anthropic.APIError as exc:
            raise review.ReviewBackendError(
                f"Claude agent review failed: {exc}", retryable=is_retryable_api_error(exc)
            ) from exc

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
