from collections.abc import AsyncIterator
from typing import Final

from pydantic import ValidationError

from code_review.config import CONFIG
from code_review.errors import ReviewBackendError
from code_review.models.findings import Finding, FindingCategory, RawFinding
from code_review.models.review import FlushCompletion
from code_review.models.severity import DiffSide, Severity

UNPARSEABLE_SNIPPET_CHARS: Final[int] = 500


async def capture_flush_marker(chunks: AsyncIterator[str], completion: FlushCompletion) -> AsyncIterator[str]:
    """Pass text chunks through while recording whether a completion-marker line appears."""

    buffer = ""

    async for chunk in chunks:
        buffer += chunk
        *lines, buffer = buffer.split("\n")
        if any(line.strip() == CONFIG["flush_complete_marker"] for line in lines):
            completion.complete = True

        yield chunk

    if buffer.strip() == CONFIG["flush_complete_marker"]:
        completion.complete = True


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


def truncated_finding_buffer(buffer: str) -> bool:
    """Return whether buffered text looks like a cut-off JSON finding."""

    return buffer.strip().startswith("{")


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
        if truncated_finding_buffer(buffer):
            raise ReviewBackendError("The review model output was truncated mid-finding.", retryable=True)

        return

    stripped = [line.strip() for line in full.splitlines()]
    has_finding_shaped_line = any(line.startswith(("{", "[")) for line in stripped)
    no_findings_markers = (
        CONFIG["no_findings_marker"],
        CONFIG["flush_complete_marker"],
        CONFIG["flush_partial_marker"],
    )
    if any(marker in stripped for marker in no_findings_markers) and not has_finding_shaped_line:
        return

    if full.strip():
        snippet = full.strip()[:UNPARSEABLE_SNIPPET_CHARS]
        raise ReviewBackendError(
            f"The review model produced unparseable output (expected JSONL findings). Output started with: {snippet}",
            retryable=True,
        )
