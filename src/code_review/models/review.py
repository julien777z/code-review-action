from enum import StrEnum

from pydantic import BaseModel, Field

from code_review.models.findings import Finding
from code_review.models.severity import Severity


class ReviewRoundResult(BaseModel):
    """The outcome of one review round and the diff snapshot it reviewed."""

    exit_code: int
    diff: str | None = None


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
        """Record where a finding was published."""

        match publication:
            case FindingPublication.INLINE:
                self.posted_any = True
            case FindingPublication.VERDICT:
                self.out_of_bounds.append(finding)
