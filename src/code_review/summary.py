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

    # Strip the section markers so untrusted model output cannot inject a second marker pair that
    # would misalign the next replacement.
    fenced = summary_text.replace(CONFIG["summary_marker_open"], "").replace(CONFIG["summary_marker_close"], "")

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


async def post_pr_summary(pr: PullRequestContext, generate: GenerateSummary) -> None:
    """Generate a description summary for the PR and merge it into the PR body."""

    if await current_head_sha(pr.repo, pr.number) != pr.head_sha:
        logger.info("Head moved since review; skipping the summary for superseded commit %s.", pr.head_sha)

        return

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

    body = await pull_request_body(pr.repo, pr.number)

    await update_pull_request_body(pr.repo, pr.number, merge_summary(body, summary_section(text)))
    logger.info("Updated PR #%s description with the generated summary.", pr.number)
