import logging
from collections.abc import Awaitable, Callable

from code_review.config import CONFIG, DISCLAIMER
from code_review.github import (
    current_head_sha,
    pull_request_body,
    pull_request_diff_if_available,
    update_pull_request_body,
)
from code_review.models.pull_request import PullRequestContext
from code_review.prompt import summary_prompt

logger = logging.getLogger("code_review.summary")

GenerateSummary = Callable[[str], Awaitable[str]]


class SummaryGenerationError(Exception):
    """Raised when the summary model returns empty or unusable output."""


def strip_summary_markers(summary_text: str) -> str:
    """Remove marker tokens that would let model output forge summary block boundaries."""

    clean = summary_text
    for token in (
        CONFIG["summary_marker_open"],
        CONFIG["summary_marker_close"],
        CONFIG["untrusted_input_open"],
        CONFIG["untrusted_input_close"],
    ):
        clean = clean.replace(token, "")

    return clean


def summary_section(summary_text: str) -> str:
    """Render the marker-delimited summary block appended to the PR description."""

    fenced = strip_summary_markers(summary_text)

    return (
        f"{CONFIG['summary_marker_open']}\n"
        "---\n"
        f"{CONFIG['untrusted_input_open']}\n"
        f"{fenced}\n"
        f"{CONFIG['untrusted_input_close']}\n\n"
        f"{DISCLAIMER}\n"
        f"{CONFIG['summary_marker_close']}"
    )


def merge_summary(body: str, section: str) -> str:
    """Merge the summary section into the PR body, replacing an existing one and preserving other text."""

    open_marker = CONFIG["summary_marker_open"]
    close_marker = CONFIG["summary_marker_close"]
    start = body.find(open_marker)
    end = body.find(close_marker, start + len(open_marker)) if start != -1 else -1

    if start != -1 and end != -1:
        return body[:start] + section + body[end + len(close_marker) :]

    if not body.strip():
        return section

    return f"{body}\n\n{section}"


async def write_summary_if_current(pr: PullRequestContext, section: str) -> None:
    """Merge and write the summary only if the PR still points at the reviewed head."""

    if await current_head_sha(pr.repo, pr.number) != pr.head_sha:
        logger.info("Head moved during summary generation; skipping the summary for superseded commit %s.", pr.head_sha)

        return

    body = await pull_request_body(pr.repo, pr.number)

    if await current_head_sha(pr.repo, pr.number) != pr.head_sha:
        logger.info("Head moved before summary update; skipping the summary for superseded commit %s.", pr.head_sha)

        return

    await update_pull_request_body(pr.repo, pr.number, merge_summary(body, section))
    logger.info("Updated PR #%s description with the generated summary.", pr.number)


async def post_pr_summary(pr: PullRequestContext, generate: GenerateSummary, *, diff: str | None = None) -> None:
    """Generate a description summary for the PR and merge it into the PR body."""

    if await current_head_sha(pr.repo, pr.number) != pr.head_sha:
        logger.info("Head moved since review; skipping the summary for superseded commit %s.", pr.head_sha)

        return

    if diff is None:
        diff = await pull_request_diff_if_available(pr.repo, pr.number)
        if diff is None:
            logger.info("PR #%s diff is too large to summarize; skipping the summary.", pr.number)

            return

    text = (await generate(summary_prompt(pr, diff))).strip()
    if not text:
        raise SummaryGenerationError("The summary model returned no output.")

    await write_summary_if_current(pr, summary_section(text))
