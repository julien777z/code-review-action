from collections.abc import AsyncIterator, Awaitable, Callable
from enum import StrEnum
from typing import TypedDict

from code_review.models.findings import Finding
from code_review.models.pull_request import PullRequestContext, ReviewInputs

ReviewTextStream = Callable[[PullRequestContext, ReviewInputs], AsyncIterator[str]]
BackendRetryable = Callable[[Exception], bool]
GenerateSummary = Callable[[str], Awaitable[str]]


class Backend(StrEnum):
    """The concrete backend resolved for this run."""

    CURSOR = "cursor"
    CLAUDE = "claude"


class BackendHandlers(TypedDict):
    """Backend behavior and error policy used by the shared runner."""

    review_text: ReviewTextStream
    generate_summary: GenerateSummary
    errors: tuple[type[Exception], ...]
    retryable: BackendRetryable
    label: str


GetBackendFindings = Callable[[ReviewInputs], AsyncIterator[Finding]]
