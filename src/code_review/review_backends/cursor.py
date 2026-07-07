import logging
from collections.abc import AsyncIterator
from typing import Final

from cursor_sdk import (
    AsyncAgent,
    AsyncClient,
    CloudAgentOptions,
    CloudRepository,
    CursorAgentError,
    ModelSelection,
)

from code_review import review
from code_review.config import CONFIG, SETTINGS
from code_review.models.shared.findings import Finding
from code_review.models.shared.pull_request import PullRequestContext, ReviewInputs
from code_review.prompt import cursor_prompt
from code_review.review_backends.jsonl import iter_findings

logger = logging.getLogger("code_review.cursor")

BRIDGE_LAUNCH_ATTEMPTS: Final[int] = 3


async def launch_bridge_with_retry() -> AsyncClient:
    """Launch the Cursor bridge, retrying the rare startup failure from a dash-leading callback token."""

    # TODO: remove this retry once cursor-sdk no longer emits a dash-leading tool-callback token.
    # cursor-sdk mints a random tool-callback auth token and passes it as a bare CLI value; the ~1.5%
    # of tokens that start with "-" make the bridge's arg parser reject it. Each launch mints a fresh
    # token, so retrying clears the transient failure.
    for _ in range(BRIDGE_LAUNCH_ATTEMPTS - 1):
        try:
            return await AsyncClient.launch_bridge()
        except CursorAgentError as exc:
            logger.warning("Cursor bridge launch failed; retrying: %s", exc)

    return await AsyncClient.launch_bridge()


def cursor_error_message(exc: CursorAgentError) -> str:
    """Return a clear failure message, explaining missing repo access when that is the cause."""

    if "does not have access" in str(exc).lower():
        return (
            "Cursor's GitHub integration cannot access this repository, so the review agent could not "
            "clone it to load the project rules. Grant Cursor access to this repository, or set "
            f"enforce-project-rules to false. Original error: {exc}"
        )

    return f"Cursor agent run failed: {exc}"


async def run_agent(prompt: str, repo: CloudRepository | None = None) -> AsyncIterator[str]:
    """Launch the Cursor agent on the standard variant and stream its reply text in chunks."""

    client = await launch_bridge_with_retry()

    # Composer defaults to the "fast" variant; pick the non-default (standard) tier instead.
    catalog = await client.list_models(api_key=SETTINGS.cursor_api_key)
    sdk_model = next((entry for entry in catalog if entry.id == SETTINGS.cursor_model), None)
    standard_variant = next(
        (variant for variant in (sdk_model.variants if sdk_model else ()) if not variant.is_default),
        None,
    )
    model_selection: str | ModelSelection = (
        ModelSelection(id=SETTINGS.cursor_model, params=list(standard_variant.params))
        if standard_variant is not None
        else SETTINGS.cursor_model
    )

    cloud = CloudAgentOptions(repos=[repo]) if repo is not None else CloudAgentOptions()
    agent = await AsyncAgent.create(
        client=client, model=model_selection, api_key=SETTINGS.cursor_api_key, cloud=cloud
    )

    try:
        run = await agent.send(prompt)
        async for chunk in run.iter_text():
            yield chunk
    finally:
        await agent.close()


async def run_cursor_review(pr: PullRequestContext) -> int:
    """Review the PR with the Cursor backend, streaming each finding as the agent emits it."""

    repo = (
        CloudRepository(url=f"https://github.com/{pr.repo}", starting_ref=pr.head_sha)
        if SETTINGS.enforce_project_rules
        else None
    )

    async def _findings(inputs: ReviewInputs) -> AsyncIterator[Finding]:
        try:
            async for finding in iter_findings(run_agent(cursor_prompt(inputs), repo=repo)):
                yield finding
        except CursorAgentError as exc:
            raise review.ReviewBackendError(cursor_error_message(exc), retryable=exc.is_retryable) from exc

    return await review.run_review_round(pr, CONFIG["review_marker"], _findings)


async def generate_text(prompt: str) -> str:
    """Run a single-shot Cursor agent turn and return the joined reply text."""

    return "".join([chunk async for chunk in run_agent(prompt)])
