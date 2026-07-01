import json
import logging
import os
from enum import StrEnum
from pathlib import Path

from code_review.config import SETTINGS, ClaudeMode, ReviewModel
from code_review.github import add_reaction, fetch_pull_request, remove_reaction
from code_review.models.shared.github_event import GithubEvent
from code_review.models.shared.pull_request import PullRequestContext
from code_review.review_backends.claude import fire_claude_routine, run_claude_api_review
from code_review.review_backends.cursor import run_cursor_review

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("code_review")

PULL_REQUEST_ACTIONS = ("opened", "synchronize", "ready_for_review")
FIRST_REVIEW_ACTIONS = ("opened", "ready_for_review")


class Backend(StrEnum):
    """The concrete backend resolved for this run."""

    CURSOR = "cursor"
    CLAUDE_API = "claude_api"
    CLAUDE_ROUTINE = "claude_routine"


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


def is_eligible(event_name: str, event: GithubEvent, repo: str) -> bool:
    """Return whether this event should trigger a review (fork, bot, association, and phrase gates)."""

    sender_type = event.sender.type if event.sender else None
    if sender_type == "Bot":
        return False

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


def claude_available() -> bool:
    """Return whether Claude can run in the configured mode (api key, or routine creds)."""

    if SETTINGS.claude_mode is ClaudeMode.ROUTINE:
        return bool(SETTINGS.claude_routine_api_key and SETTINGS.claude_routine_id)

    return bool(SETTINGS.anthropic_api_key)


def claude_backend() -> Backend:
    """Map the Claude mode to its concrete backend."""

    return Backend.CLAUDE_ROUTINE if SETTINGS.claude_mode is ClaudeMode.ROUTINE else Backend.CLAUDE_API


def select_backend(first_review: bool) -> Backend | None:
    """Pick the backend for this event, resolving `auto` and skipping when creds are missing."""

    requested = (
        SETTINGS.first_review_model
        if first_review and SETTINGS.first_review_model is not None
        else SETTINGS.review_model
    )

    match requested:
        case ReviewModel.AUTO:
            if claude_available():
                return claude_backend()

            return Backend.CURSOR if SETTINGS.cursor_api_key else None
        case ReviewModel.CLAUDE:
            return claude_backend() if claude_available() else None
        case ReviewModel.CURSOR:
            return Backend.CURSOR if SETTINGS.cursor_api_key else None
        case _:
            return None


async def run(pr: PullRequestContext, backend: Backend, *, install_signal_handlers: bool = True) -> int:
    """Dispatch the resolved backend for the PR."""

    match backend:
        case Backend.CURSOR:
            return await run_cursor_review(pr, install_signal_handlers=install_signal_handlers)
        case Backend.CLAUDE_API:
            return await run_claude_api_review(pr, install_signal_handlers=install_signal_handlers)
        case Backend.CLAUDE_ROUTINE:
            return await fire_claude_routine(pr)


async def review_event(
    event_name: str, event: GithubEvent, repo: str, *, install_signal_handlers: bool = True
) -> int:
    """Resolve one event for the given repo, pick a backend, and run a review round (shared by the action and backend)."""

    # The caller configures the GitHub token first: the action from env, the backend from a minted
    # installation token. Everything below reads it through SETTINGS via the shared github helpers.
    if not is_eligible(event_name, event, repo):
        logger.info("Event %s (%s) is not eligible for review; skipping.", event_name, event.action)

        return 0

    pr_number = resolve_pr_number(event_name, event)
    if pr_number is None:
        logger.error("Could not determine the PR number for %s.", event_name)

        return 1

    backend = select_backend(is_first_review_event(event_name, event))
    if backend is None:
        logger.info("No review backend is configured for this event; skipping.")

        return 0

    pr = await fetch_pull_request(repo, pr_number)
    if pr.state != "OPEN":
        logger.info("PR #%s is %s, not open; skipping.", pr_number, pr.state)

        return 0

    if pr.is_draft and not SETTINGS.review_drafts:
        logger.info("PR #%s is a draft and review-drafts is disabled; skipping.", pr_number)

        return 0

    if backend is Backend.CLAUDE_ROUTINE:
        return await run(pr, backend, install_signal_handlers=install_signal_handlers)

    # React with eyes on the trigger (the comment for a manual trigger, otherwise the PR) while the
    # synchronous backends review, then remove it once the round finishes.
    subject = reaction_subject(event_name, event, repo, pr_number)
    reaction_id = await add_reaction(subject)

    try:
        return await run(pr, backend, install_signal_handlers=install_signal_handlers)
    finally:
        if reaction_id is not None:
            await remove_reaction(subject, reaction_id)


async def main() -> int:
    """Resolve the triggering event from the runner environment and review it."""

    event_name, event = load_event()
    repo = os.environ.get("GITHUB_REPOSITORY", "")

    return await review_event(event_name, event, repo)
