import logging
import subprocess
from collections.abc import Awaitable, Callable

from code_review.config import CONFIG, DISCLAIMER
from code_review.github import (
    current_head_sha,
    is_diff_too_large,
    pull_request_body,
    pull_request_diff,
    update_pull_request_body,
)
from code_review.models.shared.pull_request import PullRequestContext
from code_review.prompt import summary_prompt

logger = logging.getLogger("code_review.summary")

GenerateSummary = Callable[[str], Awaitable[str]]


class SummaryGenerationError(Exception):
    """Raised when the summary model returns empty or unusable output."""


def summary_section(summary_text: str) -> str:
    """Render the marker-delimited summary block appended to the PR description."""

    # Strip our own markers so model output cannot forge a section boundary or a closing fence.
    fenced = summary_text
    for token in (
        CONFIG["summary_marker_open"],
        CONFIG["summary_marker_close"],
        CONFIG["untrusted_input_open"],
        CONFIG["untrusted_input_close"],
    ):
        fenced = fenced.replace(token, "")

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


async def head_moved(pr: PullRequestContext) -> bool:
    """Return whether the PR head advanced past the commit this summary was generated for."""

    return await current_head_sha(pr.repo, pr.number) != pr.head_sha


async def write_summary_if_current(pr: PullRequestContext, section: str) -> None:
    """Merge and write the summary only if the PR still points at the reviewed head."""

    if await head_moved(pr):
        logger.info("Head moved during summary generation; skipping the summary for superseded commit %s.", pr.head_sha)

        return

    body = await pull_request_body(pr.repo, pr.number)

    if await head_moved(pr):
        logger.info("Head moved before summary update; skipping the summary for superseded commit %s.", pr.head_sha)

        return

    await update_pull_request_body(pr.repo, pr.number, merge_summary(body, section))
    logger.info("Updated PR #%s description with the generated summary.", pr.number)


async def post_pr_summary(pr: PullRequestContext, generate: GenerateSummary, *, diff: str | None = None) -> None:
    """Generate a description summary for the PR and merge it into the PR body."""

    if await head_moved(pr):
        logger.info("Head moved since review; skipping the summary for superseded commit %s.", pr.head_sha)

        return

    if diff is None:
        try:
            diff = await pull_request_diff(pr.repo, pr.number)
        except subprocess.CalledProcessError as exc:
            if not is_diff_too_large(exc):
                raise

            logger.info("PR #%s diff is too large to summarize; skipping the summary.", pr.number)

            return

    text = (await generate(summary_prompt(pr, diff))).strip()
    if not text:
        raise SummaryGenerationError("The summary model returned no output.")

    await write_summary_if_current(pr, summary_section(text))
