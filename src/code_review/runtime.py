import logging
import os
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Final

import anthropic
from cursor_sdk import CursorAgentError

from code_review.config import CONFIG, SETTINGS
from code_review.errors import ReviewBackendError
from code_review.github import add_reaction, fetch_pull_request, remove_reaction
from code_review.models.backend import Backend, BackendHandlers
from code_review.models.config import ReviewModel
from code_review.models.findings import Finding
from code_review.models.github_event import GithubEvent
from code_review.models.pull_request import PullRequestContext, ReviewInputs
from code_review.models.review import ReviewRoundResult
from code_review.review import run_review_round
from code_review.review_backends import claude, cursor
from code_review.utils.jsonl import iter_findings
from code_review.summary import SummaryGenerationError, post_pr_summary

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("code_review")

PULL_REQUEST_ACTIONS = ("opened", "synchronize", "ready_for_review")
FIRST_REVIEW_ACTIONS = ("opened", "ready_for_review")

SUMMARY_BASE_ERRORS: Final[tuple[type[Exception], ...]] = (SummaryGenerationError, subprocess.CalledProcessError)


def cursor_error_retryable(exc: Exception) -> bool:
    """Return whether a Cursor backend exception should be retried."""

    return isinstance(exc, CursorAgentError) and exc.is_retryable


def claude_error_retryable(exc: Exception) -> bool:
    """Return whether a Claude backend exception should be retried."""

    return isinstance(exc, anthropic.APIError) and claude.is_retryable_api_error(exc)


BACKENDS: Final[dict[Backend, BackendHandlers]] = {
    Backend.CURSOR: BackendHandlers(
        review_text=cursor.review_text,
        generate_summary=cursor.generate_text,
        errors=(CursorAgentError,),
        retryable=cursor_error_retryable,
        label="Cursor",
    ),
    Backend.CLAUDE: BackendHandlers(
        review_text=claude.review_text,
        generate_summary=claude.generate_text,
        errors=(anthropic.APIError,),
        retryable=claude_error_retryable,
        label="Claude",
    ),
}


async def backend_text_chunks(
    handlers: BackendHandlers, pr: PullRequestContext, inputs: ReviewInputs
) -> AsyncIterator[str]:
    """Stream backend text and convert declared backend failures into review errors."""

    produced = False
    try:
        async for chunk in handlers["review_text"](pr, inputs):
            produced = True
            yield chunk
    except handlers["errors"] as exc:
        raise ReviewBackendError(
            f"{handlers['label']} review failed: {exc}", retryable=handlers["retryable"](exc)
        ) from exc

    if not produced:
        raise ReviewBackendError(f"{handlers['label']} review produced no output.", retryable=True)


async def stream_backend_findings(
    handlers: BackendHandlers, pr: PullRequestContext, inputs: ReviewInputs
) -> AsyncIterator[Finding]:
    """Parse JSONL findings from the backend text stream."""

    async for finding in iter_findings(backend_text_chunks(handlers, pr, inputs)):
        yield finding


async def run_backend_review(pr: PullRequestContext, handlers: BackendHandlers) -> ReviewRoundResult:
    """Run a PR review through the shared backend policy."""

    async def _findings(inputs: ReviewInputs) -> AsyncIterator[Finding]:
        async for finding in stream_backend_findings(handlers, pr, inputs):
            yield finding

    return await run_review_round(pr, CONFIG["review_marker"], _findings)


def summary_errors(handlers: BackendHandlers) -> tuple[type[Exception], ...]:
    """Return errors that make optional summary posting fail without failing review."""

    return (*SUMMARY_BASE_ERRORS, *handlers["errors"])


def load_event() -> tuple[str, GithubEvent]:
    """Read the triggering event name and payload from the runner environment."""

    name = os.environ.get("GITHUB_EVENT_NAME", "")
    path = os.environ.get("GITHUB_EVENT_PATH", "")
    if path and Path(path).exists():
        return name, GithubEvent.model_validate_json(Path(path).read_text(encoding="utf-8"))

    return name, GithubEvent()


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

    subject = reaction_subject(event_name, event, repo, pr_number)
    reaction_id = await add_reaction(subject)

    try:
        handlers = BACKENDS[backend]
        result = await run_backend_review(pr, handlers)
        exit_code = result.exit_code
        if exit_code == 0 and first_review and SETTINGS.pr_review_summary:
            try:
                await post_pr_summary(pr, handlers["generate_summary"], diff=result.diff)
            except summary_errors(handlers) as exc:
                logger.error("Could not post the PR summary; the review still succeeded: %s", exc)

        return exit_code
    finally:
        if reaction_id is not None:
            await remove_reaction(subject, reaction_id)
