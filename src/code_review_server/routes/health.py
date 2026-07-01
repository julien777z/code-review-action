from fastapi import APIRouter

from code_review_server.models.health import HealthResponse

health_router = APIRouter(tags=["health"])


@health_router.get("/health")
async def health() -> HealthResponse:
    """Report liveness for container health checks."""

    return HealthResponse(status="ok")
