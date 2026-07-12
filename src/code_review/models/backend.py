from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from enum import StrEnum
from typing import TypedDict

from code_review.models.findings import Finding
from code_review.models.pull_request import PullRequestContext, ReviewInputs
from code_review.models.review import FlushCompletion

SessionTextStream = Callable[[], AsyncIterator[str]]
SessionFindingsStream = Callable[[], AsyncIterator[Finding]]
GenerateSummary = Callable[[str], Awaitable[str]]


class Backend(StrEnum):
    """The concrete backend resolved for this run."""

    CLAUDE = "claude"
    CODEX = "codex"


class ReviewSessionStreams(TypedDict):
    """Text streams of one live backend review session."""

    review_text: SessionTextStream
    flush_text: SessionTextStream


OpenReviewSession = Callable[
    [PullRequestContext, ReviewInputs], AbstractAsyncContextManager[ReviewSessionStreams]
]


class BackendHandlers(TypedDict):
    """Backend behavior and error policy used by the shared runner."""

    review_session: OpenReviewSession
    generate_summary: GenerateSummary
    label: str


class FindingsSession(TypedDict):
    """Parsed-findings streams of one live backend review session."""

    findings: SessionFindingsStream
    flush_findings: SessionFindingsStream
    flush_completion: FlushCompletion


GetFindingsSession = Callable[[ReviewInputs], AbstractAsyncContextManager[FindingsSession]]


class FindingsBackend(TypedDict):
    """A named findings-session provider in fallback order."""

    label: str
    reviewer: str
    open_session: GetFindingsSession
