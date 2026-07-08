from enum import StrEnum
from typing import Self, TypedDict


class ReviewModel(StrEnum):
    """Which backend reviews the PR."""

    AUTO = "auto"
    CLAUDE = "claude"
    CURSOR = "cursor"

    @classmethod
    def parse(cls, value: str) -> Self | None:
        """Parse a review-model input, returning None when the value is empty."""

        normalized = value.strip().lower()

        return cls(normalized) if normalized else None


class ReviewConfig(TypedDict):
    """Static, non-configurable runner constants."""

    review_marker: str
    no_findings_marker: str
    untrusted_input_open: str
    untrusted_input_close: str
    summary_marker_open: str
    summary_marker_close: str
    status_check_name: str
    default_claude_model: str
    default_cursor_model: str
