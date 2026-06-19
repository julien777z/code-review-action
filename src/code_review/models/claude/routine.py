from pydantic import BaseModel


class RoutineFireRequest(BaseModel):
    """Request body for firing a hosted Claude review routine."""

    text: str
