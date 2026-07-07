import logging
from collections.abc import Awaitable, Callable

from code_review.config import CONFIG, DISCLAIMER
from code_review.github import pull_request_body, pull_request_diff, update_pull_request_body
from code_review.models.shared.pull_request import PullRequestContext, ReviewInputs
from code_review.prompt import summary_prompt

logger = logging.getLogger("code_review.summary")

GenerateSummary = Callable[[str], Awaitable[str]]


class SummaryGenerationError(Exception):
    """Raised when the summary model returns empty or unusable output."""


def summary_section(summary_text: str) -> str:
    """Render the marker-delimited summary block appended to the PR description."""

    return (
        f"{CONFIG['summary_marker_open']}\n"
        "---\n"
        f"{CONFIG['untrusted_input_open']}\n"
        f"{summary_text}\n"
        f"{CONFIG['untrusted_input_close']}\n\n"
        f"{DISCLAIMER}\n"
        f"{CONFIG['summary_marker_close']}"
    )


def merge_summary(body: str, section: str) -> str:
    """Merge the summary section into the PR body, replacing an existing one and preserving other text."""

    open_marker = CONFIG["summary_marker_open"]
    close_marker = CONFIG["summary_marker_close"]
    start = body.find(open_marker)
    end = body.find(close_marker)

    if 0 <= start < end:
        return body[:start] + section + body[end + len(close_marker) :]

    if not body.strip():
        return section

    return f"{body}\n\n{section}"


async def post_pr_summary(pr: PullRequestContext, generate: GenerateSummary) -> None:
    """Generate a description summary for the PR and merge it into the PR body."""

    diff = await pull_request_diff(pr.repo, pr.number)
    text = (await generate(summary_prompt(ReviewInputs(pr=pr, diff=diff)))).strip()
    if not text:
        raise SummaryGenerationError("The summary model returned no output.")

    body = await pull_request_body(pr.repo, pr.number)

    await update_pull_request_body(pr.repo, pr.number, merge_summary(body, summary_section(text)))
    logger.info("Updated PR #%s description with the generated summary.", pr.number)
