import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Final

from fastapi import FastAPI, Header, HTTPException, Request, Response

from code_review.models.shared.github_event import GithubEvent
from code_review.runtime import is_eligible
from code_review_server.config import SERVER_SETTINGS
from code_review_server.webhook import verify_signature
from code_review_server.worker import ReviewJob, ReviewWorker

logger = logging.getLogger("code_review_server.app")

SUPPORTED_EVENTS: Final[frozenset[str]] = frozenset({"pull_request", "issue_comment"})

worker: Final[ReviewWorker] = ReviewWorker()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the single review worker for the app's lifetime and stop it on shutdown."""

    worker.start()

    try:
        yield
    finally:
        await worker.stop()


app: Final[FastAPI] = FastAPI(title="code-review-action backend", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    """Report liveness for container health checks."""

    return {"status": "ok"}


@app.post("/webhooks/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
) -> Response:
    """Verify the webhook signature, then enqueue eligible pull-request events for review under the App."""

    body = await request.body()
    if not verify_signature(body, x_hub_signature_256, SERVER_SETTINGS.github_webhook_secret):
        raise HTTPException(status_code=HTTPStatus.UNAUTHORIZED, detail="Invalid webhook signature")

    if x_github_event not in SUPPORTED_EVENTS:
        return Response(status_code=HTTPStatus.NO_CONTENT)

    event = GithubEvent.model_validate_json(body)
    repo = event.repository.full_name if event.repository else None
    installation_id = event.installation.id if event.installation else None
    if repo is None or installation_id is None:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail="Webhook is missing repository or installation")

    # Reject ineligible deliveries (bots, forks, wrong trigger phrase) before minting a token, so the
    # App's own comment events do not enqueue a no-op review round each time it posts.
    if not is_eligible(x_github_event, event, repo):
        return Response(status_code=HTTPStatus.NO_CONTENT)

    await worker.submit(ReviewJob(event_name=x_github_event, event=event, repo=repo, installation_id=installation_id))

    return Response(status_code=HTTPStatus.ACCEPTED)
