import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Final

from code_review.config import CONFIG, SETTINGS
from code_review.errors import ReviewBackendError
from code_review.github import add_reaction, fetch_pull_request, remove_reaction
from code_review.models.backend import Backend, BackendHandlers, FindingsBackend, FindingsSession
from code_review.models.config import ReviewModel
from code_review.models.github_event import GithubEvent
from code_review.models.pull_request import PullRequestContext, ReviewInputs
from code_review.models.review import FlushCompletion, ReviewRoundResult
from code_review.review.round import run_review_round
from code_review.review_backends import claude, codex
from code_review.utils.jsonl import iter_findings, iter_text_lines
from code_review.summary import SummaryGenerationError, post_pr_summary

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("code_review")

PULL_REQUEST_ACTIONS = ("opened", "synchronize", "ready_for_review")
FIRST_REVIEW_ACTIONS = ("opened", "ready_for_review")

SUMMARY_BASE_ERRORS: Final[tuple[type[Exception], ...]] = (SummaryGenerationError, ReviewBackendError)


BACKENDS: Final[dict[Backend, BackendHandlers]] = {
    Backend.CLAUDE: BackendHandlers(
        review_session=claude.review_session,
        generate_summary=claude.generate_text,
        label="Claude",
    ),
    Backend.CODEX: BackendHandlers(
        review_session=codex.review_session,
        generate_summary=codex.generate_text,
        label="Codex",
    ),
}


def model_display_name(model: str) -> str:
    """Format a configured model identifier for review-comment attribution."""

    if model.startswith("gpt-"):
        return f"GPT {model.removeprefix('gpt-').replace('-', ' ').title()}"

    return model.removeprefix("claude-").replace("-", " ").title()


def reviewer_name(provider: str) -> str:
    """Return the provider and configured model shown on its review posts."""

    model = SETTINGS.claude_model if provider == "Claude" else SETTINGS.codex_model

    return f"{provider} {model_display_name(model)}"


async def backend_text_chunks(
    handlers: BackendHandlers, chunks: AsyncIterator[str], *, require_output: bool = True
) -> AsyncIterator[str]:
    """Stream backend text and reject an empty review response."""

    produced = False
    async for chunk in chunks:
        produced = True
        yield chunk

    if require_output and not produced:
        raise ReviewBackendError(f"{handlers['label']} review produced no output.", retryable=True)


async def capture_flush_marker(chunks: AsyncIterator[str], completion: FlushCompletion) -> AsyncIterator[str]:
    """Stream text lines while consuming flush-marker lines and recording an asserted completion."""

    async for line in iter_text_lines(chunks):
        stripped = line.strip()
        if stripped == CONFIG["flush_complete_marker"]:
            completion.complete = True
        elif stripped != CONFIG["flush_partial_marker"]:
            yield line


@asynccontextmanager
async def backend_findings_session(
    handlers: BackendHandlers, inputs: ReviewInputs
) -> AsyncIterator[FindingsSession]:
    """Open a backend review session exposing parsed findings for the review and flush turns."""

    flush_completion = FlushCompletion()

    async with handlers["review_session"](inputs.pr, inputs) as session:
        yield FindingsSession(
            findings=lambda: iter_findings(backend_text_chunks(handlers, session["review_text"]())),
            flush_findings=lambda: iter_findings(
                capture_flush_marker(
                    backend_text_chunks(handlers, session["flush_text"](), require_output=False),
                    flush_completion,
                )
            ),
            flush_completion=flush_completion,
        )


async def run_backend_review(
    pr: PullRequestContext, handlers: tuple[BackendHandlers, ...], deadline: float | None = None
) -> ReviewRoundResult:
    """Run a PR review through the shared backend policy."""

    backends = tuple(
        FindingsBackend(
            label=backend["label"],
            reviewer=reviewer_name(backend["label"]),
            open_session=partial(backend_findings_session, backend),
        )
        for backend in handlers
    )

    return await run_review_round(pr, CONFIG["review_marker"], backends, deadline)


def load_event() -> tuple[str, GithubEvent]:
    """Read the triggering event name and payload from the runner environment."""

    name = os.environ.get("GITHUB_EVENT_NAME", "")
    path = os.environ.get("GITHUB_EVENT_PATH", "")
    if path and Path(path).exists():
        return name, GithubEvent.model_validate_json(Path(path).read_text(encoding="utf-8"))

    return name, GithubEvent()


def is_eligible(event_name: str, event: GithubEvent) -> bool:
    """Return whether this event should trigger a review (fork, bot-comment, and phrase gates)."""

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


def configured_backends() -> tuple[Backend, ...]:
    """Return configured backends in automatic preference order."""

    return tuple(
        backend
        for backend, configured in (
            (Backend.CLAUDE, bool(SETTINGS.claude_code_oauth_token)),
            (Backend.CODEX, bool(SETTINGS.codex_auth_json)),
        )
        if configured
    )


def select_backends() -> tuple[Backend, ...]:
    """Resolve the requested primary backend and optional usage-limit fallback."""

    configured = configured_backends()
    match SETTINGS.review_model:
        case ReviewModel.AUTO:
            selected = configured
        case ReviewModel.CLAUDE:
            selected = (Backend.CLAUDE,) if Backend.CLAUDE in configured else ()
        case ReviewModel.CODEX:
            selected = (Backend.CODEX,) if Backend.CODEX in configured else ()
        case _:
            selected = ()

    if not selected or not SETTINGS.fallback_on_usage_limit:
        return selected[:1]

    fallback = tuple(backend for backend in configured if backend != selected[0])

    return selected[:1] + fallback[:1]


async def generate_summary_with_fallback(
    handlers: tuple[BackendHandlers, ...], prompt: str
) -> str:
    """Generate summary text, switching once when subscription usage is exhausted."""

    last_error: ReviewBackendError | None = None
    for index, handler in enumerate(handlers):
        try:
            return await handler["generate_summary"](prompt)
        except ReviewBackendError as exc:
            last_error = exc
            if not exc.usage_limited or index == len(handlers) - 1:
                raise

            logger.warning(
                "%s summary usage is exhausted; retrying with %s.",
                handler["label"],
                handlers[index + 1]["label"],
            )

    if last_error is not None:
        raise last_error

    raise ReviewBackendError("No summary backend is configured.")


async def main() -> int:
    """Resolve the event, pick a backend, and run one review round."""

    review_timeout = SETTINGS.review_timeout
    deadline = (
        None if review_timeout is None else asyncio.get_running_loop().time() + review_timeout.total_seconds()
    )

    event_name, event = load_event()
    if not is_eligible(event_name, event):
        logger.info("Event %s (%s) is not eligible for review; skipping.", event_name, event.action)

        return 0

    pr_number = resolve_pr_number(event_name, event)
    if pr_number is None:
        logger.error("Could not determine the PR number for %s.", event_name)

        return 1

    first_review = is_first_review_event(event_name, event)
    backends = select_backends()
    if not backends:
        logger.info("No review backend is configured for this event; skipping.")

        return 0

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    try:
        async with asyncio.timeout_at(deadline):
            pr = await fetch_pull_request(repo, pr_number)
    except TimeoutError:
        logger.error("The review deadline expired before the pull request could be loaded.")

        return 1
    if pr.state != "OPEN":
        logger.info("PR #%s is %s, not open; skipping.", pr_number, pr.state)

        return 0

    if pr.is_draft and not SETTINGS.review_drafts:
        logger.info("PR #%s is a draft and review-drafts is disabled; skipping.", pr_number)

        return 0

    if pr.head_repository != repo:
        logger.info("PR #%s originates from a fork; skipping before provider startup.", pr_number)

        return 0

    subject = reaction_subject(event_name, event, repo, pr_number)
    reaction_id = await add_reaction(subject)

    try:
        handlers = tuple(BACKENDS[backend] for backend in backends)
        result = await run_backend_review(pr, handlers, deadline)
        exit_code = result.exit_code
        if exit_code == 0 and first_review and SETTINGS.pr_review_summary:
            try:
                async with asyncio.timeout_at(deadline):
                    await post_pr_summary(
                        pr, partial(generate_summary_with_fallback, handlers), diff=result.diff
                    )
            except TimeoutError:
                logger.warning("The review deadline expired before the PR summary could finish.")
            except SUMMARY_BASE_ERRORS as exc:
                logger.error("Could not post the PR summary; the review still succeeded: %s", exc)

        return exit_code
    finally:
        if reaction_id is not None:
            await remove_reaction(subject, reaction_id)
