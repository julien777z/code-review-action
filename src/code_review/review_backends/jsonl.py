import json
import re
from collections.abc import AsyncIterator
from typing import Final

from pydantic import ValidationError

from code_review import review
from code_review.models.shared.findings import Finding, RawFinding
from code_review.models.shared.severity import DiffSide, Severity

FENCE: Final[re.Pattern[str]] = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


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


def parse_findings_blob(text: str) -> list[Finding] | None:
    """Parse a whole non-JSONL reply (fenced, {"findings": [...]}, or a bare array); None if not JSON."""

    cleaned = text.strip()
    fenced = FENCE.search(cleaned)
    if fenced is not None:
        cleaned = fenced.group(1).strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return None

    if isinstance(data, dict):
        data = data.get("findings", [])
    if not isinstance(data, list):
        return None

    findings: list[Finding] = []
    for item in data:
        try:
            raw = RawFinding.model_validate(item)
        except ValidationError:
            continue

        finding = normalize_raw(raw)
        if finding is not None:
            findings.append(finding)

    return findings


async def iter_findings(chunks: AsyncIterator[str]) -> AsyncIterator[Finding]:
    """Yield findings from streamed JSONL chunks; recover legacy/fenced replies, raising on garbage."""

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

    # No JSONL findings parsed: recover a legacy/fenced whole-blob reply, and otherwise fail loudly on
    # non-empty unparseable output so a wrong-format reply is never mistaken for a clean (zero-finding)
    # review that would approve and resolve open threads.
    blob = parse_findings_blob(full)
    if blob is not None:
        for finding in blob:
            yield finding

        return

    if full.strip():
        raise review.ReviewBackendError(
            "The review model produced unparseable output (expected JSONL findings).", retryable=True
        )
