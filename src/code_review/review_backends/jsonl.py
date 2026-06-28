import json
import re
from collections.abc import AsyncIterator
from typing import Final

from pydantic import ValidationError

from code_review import review
from code_review.config import CONFIG
from code_review.models.shared.findings import Finding, RawFinding
from code_review.models.shared.severity import DiffSide, Severity

FENCE: Final[re.Pattern[str]] = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
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
        severity=severity,
        title=raw.title,
        body=raw.body,
    )


def parse_raw_item(item: object) -> Finding | None:
    """Validate and normalize one raw finding mapping into a Finding, or None when it is not a finding."""

    try:
        raw = RawFinding.model_validate(item)
    except ValidationError:
        return None

    return normalize_raw(raw)


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
    """Parse a whole non-JSONL reply (fenced, {"findings": [...]}, a bare array, or a bare object); None otherwise."""

    cleaned = text.strip()
    fenced = FENCE.search(cleaned)
    if fenced is not None:
        cleaned = fenced.group(1).strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return None

    if isinstance(data, dict):
        if "findings" in data:
            data = data["findings"]
        else:
            finding = parse_raw_item(data)

            return [finding] if finding is not None else None

    if not isinstance(data, list):
        return None

    return [finding for item in data if (finding := parse_raw_item(item)) is not None]


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

    if CONFIG["no_findings_marker"] in full:
        return

    if full.strip():
        snippet = full.strip()[:UNPARSEABLE_SNIPPET_CHARS]
        raise review.ReviewBackendError(
            f"The review model produced unparseable output (expected JSONL findings). Output started with: {snippet}",
            retryable=True,
        )
