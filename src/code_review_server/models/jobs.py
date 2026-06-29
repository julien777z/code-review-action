from pydantic import BaseModel

from code_review.models.shared.github_event import GithubEvent


class ReviewJob(BaseModel):
    """A queued webhook delivery to review: the event name, parsed payload, repo, and installation id."""

    event_name: str
    event: GithubEvent
    repo: str
    installation_id: int
