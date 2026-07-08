from enum import StrEnum
from typing import Final, Self

from pydantic import BaseModel

from code_review.models.shared.severity import DiffSide, Severity


class FindingCategory(StrEnum):
    """Base finding category shown below review comments."""

    BUG = "bug"
    CODE_SIMPLIFICATION = "code_simplification"
    SECURITY = "security"
    PERFORMANCE = "performance"
    TESTING = "testing"
    DOCUMENTATION = "documentation"
    PROJECT_RULE = "project_rule"
    OTHER = "other"

    @classmethod
    def from_str(cls, value: str) -> Self:
        """Parse category names from snake-case, hyphenated, or display-label text."""

        normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
        try:
            return cls(normalized)
        except ValueError:
            return CATEGORY_ALIASES.get(normalized, cls.OTHER)

    @property
    def label(self) -> str:
        """Return the human-facing category label."""

        return self.value.replace("_", " ").title()


CATEGORY_ALIASES: Final[dict[str, FindingCategory]] = {
    "reliability": FindingCategory.BUG,
    "maintainability": FindingCategory.CODE_SIMPLIFICATION,
}


class Finding(BaseModel):
    """A single review finding anchored to a diff line."""

    path: str
    line: int
    side: DiffSide = DiffSide.RIGHT
    category: FindingCategory = FindingCategory.BUG
    severity: Severity
    title: str
    body: str


class RawFinding(BaseModel):
    """A finding as a backend streams it on one JSONL line, before severity/side normalization."""

    path: str
    line: int
    side: str = "RIGHT"
    category: str = FindingCategory.BUG.value
    severity: str
    title: str
    body: str


class ReviewComment(BaseModel):
    """An inline review comment to post on the PR."""

    path: str
    line: int
    side: DiffSide
    body: str


class ReviewCommentRequest(BaseModel):
    """A single inline review comment posted on its own to the PR comments endpoint."""

    commit_id: str
    path: str
    line: int
    side: DiffSide
    body: str


class ReviewPayload(BaseModel):
    """A review payload (verdict event, summary body, and any inline comments) for the reviews endpoint."""

    commit_id: str
    event: str
    body: str
    comments: list[ReviewComment]
