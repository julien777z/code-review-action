import logging
from collections.abc import AsyncIterator
from io import StringIO
from typing import Final

from cursor_sdk import AsyncAgent, AsyncClient, CursorAgentError, LocalAgentOptions, ModelSelection

from code_review.config import SETTINGS
from code_review.models.pull_request import PullRequestContext, ReviewInputs
from code_review.prompt import cursor_prompt

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


async def run_agent(prompt: str, *, load_project_rules: bool = False) -> AsyncIterator[str]:
    """Launch a local Cursor agent on the standard variant and stream its reply text in chunks."""

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

    local = LocalAgentOptions(setting_sources=["project"] if load_project_rules else [])
    agent = await AsyncAgent.create(
        client=client, model=model_selection, api_key=SETTINGS.cursor_api_key, local=local
    )

    try:
        run = await agent.send(prompt)
        async for chunk in run.iter_text():
            yield chunk
    finally:
        await agent.close()


async def review_text(pr: PullRequestContext, inputs: ReviewInputs) -> AsyncIterator[str]:
    """Stream Cursor's review reply text for the shared runner."""

    async for chunk in run_agent(
        cursor_prompt(inputs),
        load_project_rules=SETTINGS.enforce_project_rules or SETTINGS.simplify_nearby_code,
    ):
        yield chunk


async def generate_text(prompt: str) -> str:
    """Run a single-shot Cursor agent turn and return the joined reply text."""

    output = StringIO()
    async for chunk in run_agent(prompt):
        output.write(chunk)

    return output.getvalue()
