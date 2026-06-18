import os
import re
from enum import StrEnum
from typing import Final, Self, TypedDict

from pydantic import BaseModel

from code_review.models.shared.severity import Severity

ROUTINE_URL_PATTERN: Final[re.Pattern[str]] = re.compile(r"/routines/([^/]+)/fire")


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


class ClaudeMode(StrEnum):
    """How the Claude backend runs."""

    API = "api"
    ROUTINE = "routine"


class ReviewConfig(TypedDict):
    """Static, non-configurable runner constants."""

    routine_host: str
    anthropic_version: str
    routine_beta: str
    cursor_marker: str
    claude_marker: str
    status_check_name: str


CONFIG: Final[ReviewConfig] = ReviewConfig(
    routine_host="https://api.anthropic.com/v1/claude_code/routines",
    anthropic_version="2023-06-01",
    routine_beta="experimental-cc-routine-2026-04-01",
    cursor_marker="<!-- code-review:cursor -->",
    claude_marker="<!-- code-review:claude -->",
    status_check_name="Approval Verdict",
)


def resolve_routine_id(routine_id: str, routine_url: str) -> str | None:
    """Resolve the routine id from an explicit id or a fire URL; the two are mutually exclusive."""

    routine_id = routine_id.strip()
    routine_url = routine_url.strip()

    if routine_id and routine_url:
        raise ValueError("Set only one of claude-routine-id or claude-routine-url, not both.")

    if routine_id:
        return routine_id

    if not routine_url:
        return None

    match = ROUTINE_URL_PATTERN.search(routine_url)
    if match is None:
        raise ValueError(f"Could not parse a routine id from claude-routine-url: {routine_url}")

    return match.group(1)


def split_list(value: str) -> tuple[str, ...]:
    """Split a comma/newline-separated input into a tuple of non-empty items."""

    return tuple(part.strip() for part in re.split(r"[,\n]", value) if part.strip())


def parse_bool(value: str) -> bool:
    """Parse a boolean action input."""

    return value.strip().lower() in ("1", "true", "yes", "on")


class Settings(BaseModel):
    """Runtime configuration assembled from the action inputs (environment)."""

    github_token: str
    anthropic_api_key: str
    cursor_api_key: str
    claude_routine_api_key: str
    claude_routine_id: str | None
    review_model: ReviewModel
    first_review_model: ReviewModel | None
    claude_mode: ClaudeMode
    claude_model: str
    cursor_model: str
    additional_context: str
    approval_include: frozenset[Severity]
    approval_disable: bool
    min_severity: Severity
    low_findings_cap: int
    max_findings: int | None
    include_paths: tuple[str, ...]
    exclude_paths: tuple[str, ...]
    trigger_phrase: str
    review_drafts: bool
    author_associations: tuple[str, ...]
    pr_number: int | None

    @classmethod
    def from_env(cls) -> Self:
        """Build settings from the environment populated by the composite action."""

        max_findings_raw = os.environ.get("MAX_FINDINGS", "").strip()
        pr_number_raw = os.environ.get("PR_NUMBER", "").strip()
        approval_include = frozenset(
            Severity.from_str(item)
            for item in split_list(os.environ.get("APPROVAL_INCLUDE", "critical"))
        )

        return cls(
            github_token=os.environ.get("GITHUB_TOKEN", ""),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            cursor_api_key=os.environ.get("CURSOR_API_KEY", ""),
            claude_routine_api_key=os.environ.get("CLAUDE_ROUTINE_API_KEY", ""),
            claude_routine_id=resolve_routine_id(
                os.environ.get("CLAUDE_ROUTINE_ID", ""),
                os.environ.get("CLAUDE_ROUTINE_URL", ""),
            ),
            review_model=ReviewModel.parse(os.environ.get("REVIEW_MODEL", "auto")) or ReviewModel.AUTO,
            first_review_model=ReviewModel.parse(os.environ.get("FIRST_REVIEW_MODEL", "")),
            claude_mode=ClaudeMode((os.environ.get("CLAUDE_MODE", "api").strip().lower()) or "api"),
            claude_model=os.environ.get("CLAUDE_MODEL", "claude-opus-4-8").strip() or "claude-opus-4-8",
            cursor_model=os.environ.get("CURSOR_MODEL", "composer-2.5").strip() or "composer-2.5",
            additional_context=os.environ.get("ADDITIONAL_CONTEXT", "").strip(),
            approval_include=approval_include or frozenset({Severity.CRITICAL}),
            approval_disable=parse_bool(os.environ.get("APPROVAL_DISABLE", "false")),
            min_severity=Severity.from_str(os.environ.get("MIN_SEVERITY", "low") or "low"),
            low_findings_cap=int(os.environ.get("LOW_FINDINGS_CAP", "3") or "3"),
            max_findings=int(max_findings_raw) if max_findings_raw else None,
            include_paths=split_list(os.environ.get("INCLUDE_PATHS", "")),
            exclude_paths=split_list(os.environ.get("EXCLUDE_PATHS", "")),
            trigger_phrase=os.environ.get("TRIGGER_PHRASE", "agent review").strip() or "agent review",
            review_drafts=parse_bool(os.environ.get("REVIEW_DRAFTS", "true")),
            author_associations=tuple(
                item.upper() for item in split_list(os.environ.get("AUTHOR_ASSOCIATIONS", ""))
            ),
            pr_number=int(pr_number_raw) if pr_number_raw else None,
        )


SETTINGS: Final[Settings] = Settings.from_env()
