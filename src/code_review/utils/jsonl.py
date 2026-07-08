from collections.abc import AsyncIterator
from typing import Final

from pydantic import ValidationError

from code_review import review
from code_review.config import CONFIG
from code_review.models.findings import Finding, FindingCategory, RawFinding
from code_review.models.severity import DiffSide, Severity

UNPARSEABLE_SNIPPET_CHARS: Final[int] = 500


def normalize_raw(raw: RawFinding) -> Finding | None:
    """Normalize a raw finding's severity and side into a Finding, or None when the severity is unknown."""

    try:
        severity = Severity.from_str(raw.severity)
    except ValueError:
        return None

    return Finding(
        path=raw.path,
        line=raw.line,
        side=DiffSide.from_str(raw.side),
        category=FindingCategory.from_str(raw.category),
        severity=severity,
        title=raw.title,
        body=raw.body,
    )


def parse_finding_line(line: str) -> Finding | None:
    """Parse one streamed JSONL line into a normalized Finding, or None when it is not a finding."""

    stripped = line.strip()
    if not stripped:
        return None

    try:
        raw = RawFinding.model_validate_json(stripped)
    except ValidationError:
        return None

    return normalize_raw(raw)


async def iter_findings(chunks: AsyncIterator[str]) -> AsyncIterator[Finding]:
    """Yield findings from streamed JSONL chunks, raising on non-JSONL output."""

    buffer = ""
    full = ""
    produced = False

    async for chunk in chunks:
        full += chunk
        buffer += chunk
        *lines, buffer = buffer.split("\n")
        for line in lines:
            finding = parse_finding_line(line)
            if finding is not None:
                produced = True
                yield finding

    trailing = parse_finding_line(buffer)
    if trailing is not None:
        produced = True
        yield trailing
        buffer = ""

    if produced:
        # A leftover that opens a JSON object is a finding the stream cut off mid-line (for example a
        # max-tokens truncation); fail loudly rather than dropping it after earlier findings posted.
        if buffer.strip().startswith("{"):
            raise review.ReviewBackendError("The review model output was truncated mid-finding.", retryable=True)

        return

    stripped = [line.strip() for line in full.splitlines()]
    has_finding_shaped_line = any(line.startswith(("{", "[")) for line in stripped)
    if CONFIG["no_findings_marker"] in stripped and not has_finding_shaped_line:
        return

    if full.strip():
        snippet = full.strip()[:UNPARSEABLE_SNIPPET_CHARS]
        raise review.ReviewBackendError(
            f"The review model produced unparseable output (expected JSONL findings). Output started with: {snippet}",
            retryable=True,
        )
