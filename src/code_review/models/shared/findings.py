from pydantic import BaseModel

from code_review.models.shared.severity import DiffSide, Severity


class Finding(BaseModel):
    """A single review finding anchored to a diff line."""

    path: str
    line: int
    side: DiffSide = DiffSide.RIGHT
    severity: Severity
    title: str
    body: str


class RawFinding(BaseModel):
    """A finding as a backend streams it on one JSONL line, before severity/side normalization."""

    path: str
    line: int
    side: str = "RIGHT"
    severity: str
    title: str
    body: str


class ReviewComment(BaseModel):
    """An inline review comment to post on the PR."""

    path: str
    line: int
    side: DiffSide
    body: str


class ReviewCommentRequest(BaseModel):
    """A single inline review comment posted on its own to the PR comments endpoint."""

    commit_id: str
    path: str
    line: int
    side: DiffSide
    body: str


class ReviewPayload(BaseModel):
    """A review payload (verdict event, summary body, and any inline comments) for the reviews endpoint."""

    commit_id: str
    event: str
    body: str
    comments: list[ReviewComment]
