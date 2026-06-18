import asyncio
import json
import logging
import re
from typing import Final

from cursor_sdk import AsyncAgent, AsyncClient, CloudAgentOptions, CursorAgentError, ModelSelection
from pydantic import ValidationError

from code_review import review
from code_review.config import CONFIG, SETTINGS
from code_review.models.cursor.reply import CursorReply
from code_review.models.shared.findings import Finding
from code_review.models.shared.pull_request import PullRequestContext, ReviewInputs
from code_review.models.shared.severity import DiffSide, Severity
from code_review.prompt import cursor_prompt

logger = logging.getLogger("code_review.cursor")

FENCE: Final[re.Pattern[str]] = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)


def parse_cursor_reply(text: str) -> list[Finding]:
    """Parse the Cursor agent's JSON reply into normalized findings."""

    cleaned = text.strip()
    fenced = FENCE.search(cleaned)
    if fenced is not None:
        cleaned = fenced.group(1)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise review.ReviewBackendError(f"Could not parse the Cursor reply: {exc}") from exc

    if isinstance(data, list):
        data = {"findings": data}

    try:
        reply = CursorReply.model_validate(data)
    except ValidationError as exc:
        raise review.ReviewBackendError(f"Unexpected Cursor findings shape: {exc}") from exc

    findings: list[Finding] = []
    for raw in reply.findings:
        try:
            severity = Severity.from_str(raw.severity)
        except ValueError:
            continue

        findings.append(
            Finding(
                path=raw.path,
                line=raw.line,
                side=DiffSide.from_str(raw.side),
                severity=severity,
                title=raw.title,
                body=raw.body,
            )
        )

    return findings


async def run_agent(prompt: str) -> str:
    """Launch the Cursor agent on the standard variant in non-fast mode and return its reply text."""

    client = await AsyncClient.launch_bridge()

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

    agent = await AsyncAgent.create(
        client=client, model=model_selection, api_key=SETTINGS.cursor_api_key, cloud=CloudAgentOptions()
    )

    try:
        run = await agent.send(prompt)

        return await run.text()
    finally:
        await agent.close()


def run_cursor_review(pr: PullRequestContext) -> int:
    """Review the PR with the Cursor backend and post the result."""

    def _findings(inputs: ReviewInputs) -> list[Finding]:
        prompt = cursor_prompt(inputs)
        try:
            reply = asyncio.run(run_agent(prompt))
        except CursorAgentError as exc:
            raise review.ReviewBackendError(f"Cursor agent run failed: {exc}") from exc

        return parse_cursor_reply(reply)

    return review.run_review_round(pr, CONFIG["review_marker"], _findings)
