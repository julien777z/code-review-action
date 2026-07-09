from pydantic import BaseModel, ConfigDict


class EventRepo(BaseModel):
    """Repository reference on a PR head."""

    model_config = ConfigDict(extra="ignore")

    full_name: str | None = None


class EventPullRequestHead(BaseModel):
    """Head ref of a pull request event."""

    model_config = ConfigDict(extra="ignore")

    repo: EventRepo | None = None
    sha: str | None = None
    ref: str | None = None


class EventPullRequest(BaseModel):
    """Pull request payload on a `pull_request` event."""

    model_config = ConfigDict(extra="ignore")

    number: int | None = None
    head: EventPullRequestHead | None = None
    draft: bool = False


class EventComment(BaseModel):
    """Comment payload on an `issue_comment` event."""

    model_config = ConfigDict(extra="ignore")

    id: int | None = None
    body: str = ""


class IssuePullRequestRef(BaseModel):
    """Marker object present when an issue is actually a pull request."""

    model_config = ConfigDict(extra="ignore")

    url: str | None = None


class EventIssue(BaseModel):
    """Issue payload on an `issue_comment` event."""

    model_config = ConfigDict(extra="ignore")

    number: int | None = None
    pull_request: IssuePullRequestRef | None = None


class EventSender(BaseModel):
    """Actor that triggered the event."""

    model_config = ConfigDict(extra="ignore")

    type: str | None = None


class GithubEvent(BaseModel):
    """The subset of the GitHub event payload the runner reads."""

    model_config = ConfigDict(extra="ignore")

    action: str | None = None
    pull_request: EventPullRequest | None = None
    issue: EventIssue | None = None
    comment: EventComment | None = None
    sender: EventSender | None = None
