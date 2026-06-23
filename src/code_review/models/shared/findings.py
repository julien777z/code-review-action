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


class ReviewComment(BaseModel):
    """An inline review comment to post on the PR."""

    path: str
    line: int
    side: DiffSide
    body: str


class ReviewPayload(BaseModel):
    """The full review payload posted in one GitHub API call."""

    commit_id: str
    event: str
    body: str
    comments: list[ReviewComment]
