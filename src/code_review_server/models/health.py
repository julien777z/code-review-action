from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Liveness payload returned to container health checks."""

    status: str
