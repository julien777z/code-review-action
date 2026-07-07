import json
import logging
import os
import subprocess
from collections.abc import Awaitable, Callable
from enum import StrEnum
from pathlib import Path
from typing import Final, TypedDict

import anthropic
from cursor_sdk import CursorAgentError

from code_review.config import SETTINGS, ReviewModel
from code_review.github import add_reaction, fetch_pull_request, remove_reaction
from code_review.models.shared.github_event import GithubEvent
from code_review.models.shared.pull_request import PullRequestContext
from code_review.review_backends import claude, cursor
from code_review.summary import GenerateSummary, SummaryGenerationError, post_pr_summary

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("code_review")

PULL_REQUEST_ACTIONS = ("opened", "synchronize", "ready_for_review")
FIRST_REVIEW_ACTIONS = ("opened", "ready_for_review")

SUMMARY_ERRORS: Final[tuple[type[Exception], ...]] = (
    SummaryGenerationError,
    subprocess.CalledProcessError,
    anthropic.APIError,
    CursorAgentError,
)

RunReview = Callable[[PullRequestContext], Awaitable[int]]


class Backend(StrEnum):
    """The concrete backend resolved for this run."""

    CURSOR = "cursor"
    CLAUDE = "claude"


class BackendHandlers(TypedDict):
    """The review runner and summary generator a backend dispatches to."""

    run_review: RunReview
    generate_summary: GenerateSummary


BACKENDS: Final[dict[Backend, BackendHandlers]] = {
    Backend.CURSOR: BackendHandlers(
        run_review=cursor.run_cursor_review,
        generate_summary=cursor.generate_text,
    ),
    Backend.CLAUDE: BackendHandlers(
        run_review=claude.run_claude_api_review,
        generate_summary=claude.generate_text,
    ),
}


def load_event() -> tuple[str, GithubEvent]:
    """Read the triggering event name and payload from the runner environment."""

    name = os.environ.get("GITHUB_EVENT_NAME", "")
    path = os.environ.get("GITHUB_EVENT_PATH", "")
    payload: dict[str, object] = {}
    if path and Path(path).exists():
        payload = json.loads(Path(path).read_text(encoding="utf-8"))

    return name, GithubEvent.model_validate(payload)


def association_allowed(association: str | None) -> bool:
    """Return whether the actor's association passes the allowlist (empty allowlist = everyone)."""

    if not SETTINGS.author_associations:
        return True

    return bool(association) and association.upper() in SETTINGS.author_associations


def is_eligible(event_name: str, event: GithubEvent) -> bool:
    """Return whether this event should trigger a review (fork, bot-comment, association, and phrase gates)."""

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    sender_type = event.sender.type if event.sender else None

    match event_name:
        case "pull_request":
            pull_request = event.pull_request
            head_repo = pull_request.head.repo if pull_request and pull_request.head else None

            return bool(
                pull_request
                and event.action in PULL_REQUEST_ACTIONS
                and head_repo is not None
                and head_repo.full_name == repo
                and association_allowed(pull_request.author_association)
            )
        case "issue_comment":
            issue = event.issue
            comment = event.comment
            body = comment.body.strip().lower() if comment else ""

            return bool(
                issue
                and issue.pull_request is not None
                and comment
                and sender_type != "Bot"
                and body.startswith(SETTINGS.trigger_phrase.lower())
                and association_allowed(comment.author_association)
            )
        case "workflow_dispatch":
            return True
        case _:
            return False


def resolve_pr_number(event_name: str, event: GithubEvent) -> int | None:
    """Determine which PR number this event targets."""

    match event_name:
        case "pull_request":
            return event.pull_request.number if event.pull_request else None
        case "issue_comment":
            return event.issue.number if event.issue else None
        case "workflow_dispatch":
            return SETTINGS.pr_number
        case _:
            return None


def is_first_review_event(event_name: str, event: GithubEvent) -> bool:
    """Return whether this event is the PR's first review (opened or marked ready)."""

    return event_name == "pull_request" and event.action in FIRST_REVIEW_ACTIONS


def reaction_subject(event_name: str, event: GithubEvent, repo: str, pr_number: int) -> str:
    """Return the API path to react on: the trigger comment for a manual trigger, otherwise the PR."""

    if event_name == "issue_comment" and event.comment is not None and event.comment.id is not None:
        return f"repos/{repo}/issues/comments/{event.comment.id}"

    return f"repos/{repo}/issues/{pr_number}"


def select_backend(first_review: bool) -> Backend | None:
    """Pick the backend for this event, resolving `auto` and skipping when creds are missing."""

    requested = (
        SETTINGS.first_review_model
        if first_review and SETTINGS.first_review_model is not None
        else SETTINGS.review_model
    )

    match requested:
        case ReviewModel.AUTO:
            if SETTINGS.anthropic_api_key:
                return Backend.CLAUDE

            return Backend.CURSOR if SETTINGS.cursor_api_key else None
        case ReviewModel.CLAUDE:
            return Backend.CLAUDE if SETTINGS.anthropic_api_key else None
        case ReviewModel.CURSOR:
            return Backend.CURSOR if SETTINGS.cursor_api_key else None
        case _:
            return None


async def main() -> int:
    """Resolve the event, pick a backend, and run one review round."""

    event_name, event = load_event()
    if not is_eligible(event_name, event):
        logger.info("Event %s (%s) is not eligible for review; skipping.", event_name, event.action)

        return 0

    pr_number = resolve_pr_number(event_name, event)
    if pr_number is None:
        logger.error("Could not determine the PR number for %s.", event_name)

        return 1

    first_review = is_first_review_event(event_name, event)
    backend = select_backend(first_review)
    if backend is None:
        logger.info("No review backend is configured for this event; skipping.")

        return 0

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    pr = await fetch_pull_request(repo, pr_number)
    if pr.state != "OPEN":
        logger.info("PR #%s is %s, not open; skipping.", pr_number, pr.state)

        return 0

    if pr.is_draft and not SETTINGS.review_drafts:
        logger.info("PR #%s is a draft and review-drafts is disabled; skipping.", pr_number)

        return 0

    # React with eyes on the trigger (the comment for a manual trigger, otherwise the PR) while the
    # backend reviews, then remove it once the round finishes.
    subject = reaction_subject(event_name, event, repo, pr_number)
    reaction_id = await add_reaction(subject)

    try:
        handlers = BACKENDS[backend]
        exit_code = await handlers["run_review"](pr)
        if exit_code == 0 and first_review and SETTINGS.pr_review_summary:
            try:
                await post_pr_summary(pr, handlers["generate_summary"])
            except SUMMARY_ERRORS as exc:
                logger.error("Could not post the PR summary; the review still succeeded: %s", exc)

        return exit_code
    finally:
        if reaction_id is not None:
            await remove_reaction(subject, reaction_id)
