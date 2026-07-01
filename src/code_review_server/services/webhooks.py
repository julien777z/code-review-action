import hashlib
import hmac
import logging
from http import HTTPStatus
from typing import Final

from fastapi import HTTPException

from code_review.models.shared.github_event import GithubEvent
from code_review.runtime import is_eligible
from code_review_server.core.config import SERVER_SETTINGS
from code_review_server.models.jobs import ReviewJob
from code_review_server.services.worker import review_worker

logger = logging.getLogger(__name__)

SIGNATURE_PREFIX: Final[str] = "sha256="

SUPPORTED_EVENTS: Final[frozenset[str]] = frozenset({"pull_request", "issue_comment"})


def verify_signature(body: bytes, signature_header: str | None, secret: str) -> bool:
    """Verify the X-Hub-Signature-256 HMAC of the raw webhook body against the shared secret."""

    if not secret or not signature_header or not signature_header.startswith(SIGNATURE_PREFIX):
        return False

    expected = f"{SIGNATURE_PREFIX}{hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()}"
    if len(signature_header) != len(expected):
        return False

    return hmac.compare_digest(expected, signature_header)


async def process_github_delivery(body: bytes, signature_header: str | None, event_name: str | None) -> HTTPStatus:
    """Verify a webhook delivery, drop ineligible ones, and enqueue eligible reviews under the App."""

    if not verify_signature(body, signature_header, SERVER_SETTINGS.github_webhook_secret):
        raise HTTPException(status_code=HTTPStatus.UNAUTHORIZED, detail="Invalid webhook signature")

    if event_name not in SUPPORTED_EVENTS:
        return HTTPStatus.NO_CONTENT

    event = GithubEvent.model_validate_json(body)
    repo = event.repository.full_name if event.repository else None
    installation_id = event.installation.id if event.installation else None
    if repo is None or installation_id is None:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail="Webhook is missing repository or installation")

    # Reject ineligible deliveries (bots, forks, wrong trigger phrase) before minting a token, so the
    # App's own comment events do not enqueue a no-op review round each time it posts.
    if not is_eligible(event_name, event, repo):
        return HTTPStatus.NO_CONTENT

    await review_worker.submit(
        ReviewJob(event_name=event_name, event=event, repo=repo, installation_id=installation_id)
    )

    return HTTPStatus.ACCEPTED
