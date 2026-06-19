from pydantic import BaseModel, Field


class CursorRawFinding(BaseModel):
    """A finding as the Cursor agent emits it, before normalization."""

    path: str
    line: int
    side: str = "RIGHT"
    severity: str
    title: str
    body: str


class CursorReply(BaseModel):
    """The Cursor agent's JSON reply."""

    findings: list[CursorRawFinding] = Field(default_factory=list)
