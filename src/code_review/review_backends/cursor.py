import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from io import StringIO
from typing import Final

import httpx
from cursor_sdk import (
    AgentBusyError,
    AsyncAgent,
    AsyncClient,
    AsyncRun,
    CursorAgentError,
    LocalAgentOptions,
    ModelSelection,
    UnsupportedRunOperationError,
)

from code_review.config import SETTINGS
from code_review.models.backend import ReviewSessionStreams
from code_review.models.pull_request import PullRequestContext, ReviewInputs
from code_review.prompt import cursor_prompt, flush_prompt

logger = logging.getLogger("code_review.cursor")

BRIDGE_LAUNCH_ATTEMPTS: Final[int] = 3
BRIDGE_READ_TIMEOUT_MARGIN: Final[timedelta] = timedelta(minutes=1)
BRIDGE_CONNECT_TIMEOUT: Final[timedelta] = timedelta(seconds=30)

FLUSH_SEND_ATTEMPTS: Final[int] = 5
FLUSH_SEND_RETRY_DELAY: Final[timedelta] = timedelta(seconds=0.5)


def bridge_client_timeout() -> httpx.Timeout | None:
    """Cap the bridge read timeout just past the review budget so a silent agent never trips the SDK default."""

    review_timeout = SETTINGS.review_timeout
    if review_timeout is None:
        return None

    read_seconds = (review_timeout + BRIDGE_READ_TIMEOUT_MARGIN).total_seconds()

    return httpx.Timeout(read_seconds, connect=BRIDGE_CONNECT_TIMEOUT.total_seconds())


async def launch_bridge_with_retry() -> AsyncClient:
    """Launch the Cursor bridge, retrying the rare startup failure from a dash-leading callback token."""

    client_timeout = bridge_client_timeout()

    for _ in range(BRIDGE_LAUNCH_ATTEMPTS - 1):
        try:
            return await AsyncClient.launch_bridge(client_timeout=client_timeout)
        except CursorAgentError as exc:
            logger.warning("Cursor bridge launch failed; retrying: %s", exc)

    return await AsyncClient.launch_bridge(client_timeout=client_timeout)


async def create_agent(*, load_project_rules: bool) -> AsyncAgent:
    """Launch the bridge and create a local Cursor agent on the standard model variant."""

    client = await launch_bridge_with_retry()

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

    return await AsyncAgent.create(
        client=client, model=model_selection, api_key=SETTINGS.cursor_api_key, local=local
    )


async def interrupt_run(run: AsyncRun) -> None:
    """Cancel an in-flight run, tolerating one that already reached a terminal status."""

    try:
        await run.cancel()
    except UnsupportedRunOperationError:
        logger.info("The review run had already finished; flushing without cancellation.")


async def send_flush_turn(agent: AsyncAgent) -> AsyncRun:
    """Send the wrap-up turn, waiting out the brief window where the cancelled run still holds the agent."""

    for _ in range(FLUSH_SEND_ATTEMPTS - 1):
        try:
            return await agent.send(flush_prompt())
        except AgentBusyError:
            logger.info("Cursor agent is still busy after cancellation; retrying the flush turn.")

            await asyncio.sleep(FLUSH_SEND_RETRY_DELAY.total_seconds())

    return await agent.send(flush_prompt())


@asynccontextmanager
async def review_session(pr: PullRequestContext, inputs: ReviewInputs) -> AsyncIterator[ReviewSessionStreams]:
    """Open a Cursor agent review session whose in-flight reply can be interrupted and flushed."""

    agent = await create_agent(
        load_project_rules=SETTINGS.enforce_project_rules or SETTINGS.simplify_nearby_code
    )

    try:
        run = await agent.send(cursor_prompt(inputs))

        async def _flush_text() -> AsyncIterator[str]:
            await interrupt_run(run)

            flush_run = await send_flush_turn(agent)
            async for chunk in flush_run.iter_text():
                yield chunk

        yield ReviewSessionStreams(review_text=run.iter_text, flush_text=_flush_text)
    finally:
        await agent.close()


async def run_agent(prompt: str) -> AsyncIterator[str]:
    """Run a single agent turn without project rules and stream its reply text in chunks."""

    agent = await create_agent(load_project_rules=False)

    try:
        run = await agent.send(prompt)
        async for chunk in run.iter_text():
            yield chunk
    finally:
        await agent.close()


async def generate_text(prompt: str) -> str:
    """Run a single-shot Cursor agent turn and return the joined reply text."""

    output = StringIO()
    async for chunk in run_agent(prompt):
        output.write(chunk)

    return output.getvalue()
