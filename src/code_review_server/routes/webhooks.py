from fastapi import APIRouter, Header, Request, Response

from code_review_server.services.webhooks import process_github_delivery

webhooks_router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@webhooks_router.post("/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
) -> Response:
    """Verify the webhook signature, then enqueue eligible pull-request events for review under the App."""

    body = await request.body()
    status = await process_github_delivery(body, x_hub_signature_256, x_github_event)

    return Response(status_code=status)
