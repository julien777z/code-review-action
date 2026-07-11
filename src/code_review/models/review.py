from enum import StrEnum
from typing import Final

from pydantic import BaseModel, Field

from code_review.models.findings import Finding
from code_review.models.severity import Severity


class CheckConclusion(StrEnum):
    """GitHub check-run conclusion for the review verdict."""

    SUCCESS = "success"
    NEUTRAL = "neutral"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    ACTION_REQUIRED = "action_required"


REVIEWED_CONCLUSIONS: Final[frozenset[CheckConclusion]] = frozenset(
    {CheckConclusion.SUCCESS, CheckConclusion.NEUTRAL, CheckConclusion.FAILURE}
)


class ReviewRoundResult(BaseModel):
    """The outcome of one review round and the diff snapshot it reviewed."""

    exit_code: int
    diff: str | None = None


class ReviewPhaseStats(BaseModel):
    """Arrival counters for one review phase's finding stream."""

    label: str
    received: int = 0


class FlushCompletion(BaseModel):
    """Whether the wrap-up flush turn asserted the review had covered every changed file."""

    complete: bool = False


class FindingPublication(StrEnum):
    """Where a finding was made visible."""

    INLINE = "inline"
    VERDICT = "verdict"


class RoundFindings(BaseModel):
    """Findings visible in the current review round."""

    current_keys: set[tuple[str, str]] = Field(default_factory=set)
    severity_by_key: dict[tuple[str, str], Severity] = Field(default_factory=dict)
    out_of_bounds: list[Finding] = Field(default_factory=list)
    posted_any: bool = False
    published_count: int = 0
    timed_out: bool = False

    @property
    def needs_verdict_review(self) -> bool:
        """Return whether the round created visible review content outside the check run."""

        return self.posted_any or bool(self.out_of_bounds)

    def track_current(self, title_key: tuple[str, str], finding: Finding) -> None:
        """Track this finding as current for thread reconciliation and blocking severity."""

        self.current_keys.add(title_key)
        self.severity_by_key[title_key] = finding.severity

    def track_publication(
        self, finding: Finding, publication: FindingPublication
    ) -> None:
        """Record where a finding was published and count it against the total cap."""

        self.published_count += 1

        match publication:
            case FindingPublication.INLINE:
                self.posted_any = True
            case FindingPublication.VERDICT:
                self.out_of_bounds.append(finding)
