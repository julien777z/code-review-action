from collections.abc import AsyncIterator

from pydantic import ValidationError

from code_review.models.shared.findings import Finding, RawFinding
from code_review.models.shared.severity import DiffSide, Severity


def parse_finding_line(line: str) -> Finding | None:
    """Parse one streamed JSONL line into a normalized Finding, or None when it is not a finding."""

    stripped = line.strip()
    if not stripped:
        return None

    try:
        raw = RawFinding.model_validate_json(stripped)
    except ValidationError:
        return None

    try:
        severity = Severity.from_str(raw.severity)
    except ValueError:
        return None

    return Finding(
        path=raw.path,
        line=raw.line,
        side=DiffSide.from_str(raw.side),
        severity=severity,
        title=raw.title,
        body=raw.body,
    )


async def iter_findings(chunks: AsyncIterator[str]) -> AsyncIterator[Finding]:
    """Yield findings from a stream of text chunks, parsing each complete JSONL line as it lands."""

    buffer = ""

    async for chunk in chunks:
        buffer += chunk
        *lines, buffer = buffer.split("\n")
        for line in lines:
            finding = parse_finding_line(line)
            if finding is not None:
                yield finding

    trailing = parse_finding_line(buffer)
    if trailing is not None:
        yield trailing
