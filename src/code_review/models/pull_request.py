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
    head_repo_owner: str
    head_repo_name: str
    url: str
    author: str
    is_draft: bool
    state: str

    @property
    def head_repository(self) -> str:
        """Return the complete repository identity for the PR head."""

        return f"{self.head_repo_owner}/{self.head_repo_name}" if self.head_repo_owner and self.head_repo_name else ""


class PullRequestBodyUpdate(BaseModel):
    """Request body for replacing a PR's description."""

    body: str


class ReviewInputs(BaseModel):
    """Inputs a backend needs to produce findings for one round."""

    pr: PullRequestContext
    diff: str
    posted_findings: dict[str, list[PostedFinding]] = Field(default_factory=dict)
    provider_handoff: str | None = None
