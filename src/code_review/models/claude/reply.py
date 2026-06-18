from pydantic import BaseModel, Field

from code_review.models.shared.findings import Finding


class ClaudeReply(BaseModel):
    """Structured-output wrapper returned by the Claude Messages API review."""

    findings: list[Finding] = Field(default_factory=list)
