from pydantic import BaseModel, ConfigDict, Field


class ThreadCommentAuthor(BaseModel):
    """Author login on a review-thread comment."""

    login: str | None = None


class ThreadCommentNode(BaseModel):
    """First comment of a review thread."""

    author: ThreadCommentAuthor | None = None
    body: str = ""
    path: str | None = None


class ThreadComments(BaseModel):
    """Comments container for a review thread."""

    nodes: list[ThreadCommentNode] = Field(default_factory=list)


class ReviewThread(BaseModel):
    """A PR review thread as returned by the GraphQL API."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    is_resolved: bool = Field(alias="isResolved")
    is_outdated: bool = Field(alias="isOutdated")
    comments: ThreadComments
