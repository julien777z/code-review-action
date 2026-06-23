import asyncio
from collections.abc import AsyncIterator

import pytest

from code_review.models.shared.findings import Finding
from code_review.models.shared.severity import DiffSide, Severity
from code_review.review import ReviewBackendError
from code_review.review_backends.jsonl import iter_findings, parse_finding_line


async def text_stream(*parts: str) -> AsyncIterator[str]:
    """Yield the given text parts as an async chunk stream."""

    for part in parts:
        yield part


async def collect(*parts: str) -> list[Finding]:
    """Drain iter_findings over the given chunks into a list."""

    return [finding async for finding in iter_findings(text_stream(*parts))]


class TestParseFindingLine:
    """Test that a streamed JSONL line parses, normalizes, and skips non-findings."""

    def test_parses_and_normalizes_severity(self) -> None:
        """Test that a compact finding line parses with a normalized severity."""

        finding = parse_finding_line('{"path":"a.py","line":3,"side":"RIGHT","severity":"high","title":"T","body":"B"}')

        assert finding is not None
        assert finding.severity is Severity.HIGH

    def test_normalizes_left_side(self) -> None:
        """Test that a LEFT-side line normalizes the diff side."""

        finding = parse_finding_line('{"path":"a.py","line":3,"side":"LEFT","severity":"low","title":"T","body":"B"}')

        assert finding is not None
        assert finding.side is DiffSide.LEFT

    def test_capitalized_severity_normalized(self) -> None:
        """Test that a capitalized severity word is normalized to the enum."""

        finding = parse_finding_line('{"path":"a.py","line":1,"side":"RIGHT","severity":"Critical","title":"T","body":"B"}')

        assert finding is not None
        assert finding.severity is Severity.CRITICAL

    @pytest.mark.parametrize(
        "line",
        [
            '{"path":"a.py","line":1,"side":"RIGHT","severity":"bogus","title":"T","body":"B"}',
            "   ",
            "Here are the findings:",
            "```json",
            '{"findings": []}',
        ],
        ids=["unknown-severity", "blank", "prose", "fence", "wrong-shape"],
    )
    def test_skips_non_findings(self, line: str) -> None:
        """Test that an unknown-severity, blank, prose, fenced, or wrong-shape line yields no finding."""

        assert parse_finding_line(line) is None


class TestIterFindings:
    """Test that streamed chunks are reassembled into one finding per complete JSONL line."""

    def test_yields_one_finding_per_line(self) -> None:
        """Test that each complete line in the stream yields a finding."""

        first = '{"path":"a.py","line":1,"side":"RIGHT","severity":"high","title":"A","body":"B"}'
        second = '{"path":"b.py","line":2,"side":"RIGHT","severity":"low","title":"B","body":"C"}'

        findings = asyncio.run(collect(f"{first}\n{second}\n"))

        assert [finding.title for finding in findings] == ["A", "B"]

    def test_reassembles_line_split_across_chunks(self) -> None:
        """Test that a finding split across chunk boundaries is reassembled before parsing."""

        findings = asyncio.run(
            collect('{"path":"a.py","line":1,"side":"RIGHT","sev', 'erity":"high","title":"A","body":"B"}\n')
        )

        assert [finding.title for finding in findings] == ["A"]

    def test_flushes_trailing_line_without_newline(self) -> None:
        """Test that a final line with no trailing newline is still parsed."""

        findings = asyncio.run(collect('{"path":"a.py","line":1,"side":"RIGHT","severity":"high","title":"A","body":"B"}'))

        assert len(findings) == 1

    def test_skips_interleaved_prose(self) -> None:
        """Test that prose lines between findings are skipped."""

        line = '{"path":"a.py","line":1,"side":"RIGHT","severity":"high","title":"A","body":"B"}'

        findings = asyncio.run(collect(f"Let me review.\n{line}\nDone.\n"))

        assert [finding.title for finding in findings] == ["A"]

    def test_recovers_legacy_findings_object(self) -> None:
        """Test that a whole-reply {"findings": [...]} object is recovered when JSONL parsing finds none."""

        blob = '{"findings": [{"path":"a.py","line":1,"side":"RIGHT","severity":"high","title":"A","body":"B"}]}'

        findings = asyncio.run(collect(blob))

        assert [finding.title for finding in findings] == ["A"]

    def test_recovers_fenced_json_array(self) -> None:
        """Test that a fenced JSON array reply is recovered when JSONL parsing finds none."""

        blob = '```json\n[{"path":"a.py","line":1,"side":"LEFT","severity":"low","title":"A","body":"B"}]\n```'

        findings = asyncio.run(collect(blob))

        assert [finding.side for finding in findings] == [DiffSide.LEFT]

    def test_empty_findings_object_is_clean(self) -> None:
        """Test that an explicit empty {"findings": []} reply yields no findings without raising."""

        assert asyncio.run(collect('{"findings": []}')) == []

    def test_blank_output_is_clean(self) -> None:
        """Test that blank model output yields no findings without raising."""

        assert asyncio.run(collect("   \n")) == []

    def test_raises_on_unparseable_output(self) -> None:
        """Test that non-empty output with no parseable findings raises instead of approving silently."""

        with pytest.raises(ReviewBackendError):
            asyncio.run(collect("This PR looks great, no issues found!\n"))
