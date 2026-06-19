from pydantic import BaseModel, Field


class PostedFinding(BaseModel):
    """A finding already posted on the PR (severity word + title)."""

    severity: str
    title: str


class PullRequestContext(BaseModel):
    """Resolved metadata for the PR under review."""

    repo: str
    number: int
    head_sha: str
    head_ref: str
    url: str
    author: str
    is_draft: bool
    state: str


class ReviewInputs(BaseModel):
    """Inputs a backend needs to produce findings for one round."""

    pr: PullRequestContext
    diff: str
    posted_findings: dict[str, list[PostedFinding]] = Field(default_factory=dict)
