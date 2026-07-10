import asyncio
from collections.abc import AsyncIterator

import pytest

from code_review.config import CONFIG
from code_review.models.findings import Finding, FindingCategory
from code_review.models.review import FlushCompletion
from code_review.models.severity import DiffSide, Severity
from code_review.errors import ReviewBackendError
from code_review.utils.jsonl import capture_flush_marker, iter_findings, parse_finding_line


async def text_stream(*parts: str) -> AsyncIterator[str]:
    """Yield the given text parts as an async chunk stream."""

    for part in parts:
        yield part


async def collect(*parts: str) -> list[Finding]:
    """Drain iter_findings over the given chunks into a list."""

    return [finding async for finding in iter_findings(text_stream(*parts))]


async def capture(*parts: str) -> tuple[list[str], FlushCompletion]:
    """Drain capture_flush_marker over the given chunks, returning the passed-through chunks and completion."""

    completion = FlushCompletion()
    chunks = [chunk async for chunk in capture_flush_marker(text_stream(*parts), completion)]

    return chunks, completion


class TestParseFindingLine:
    """Test that a streamed JSONL line parses, normalizes, and skips non-findings."""

    def test_parses_and_normalizes_severity(self) -> None:
        """Test that a compact finding line parses with a normalized severity."""

        finding = parse_finding_line(
            '{"path":"a.py","line":3,"side":"RIGHT","category":"bug","severity":"high","title":"T","body":"B"}'
        )

        assert finding is not None
        assert finding.category is FindingCategory.BUG
        assert finding.severity is Severity.HIGH

    def test_parses_category_display_label(self) -> None:
        """Test that category labels normalize from human-facing text."""

        finding = parse_finding_line(
            '{"path":"a.py","line":3,"side":"RIGHT","category":"Code Simplification","severity":"low","title":"T","body":"B"}'
        )

        assert finding is not None
        assert finding.category is FindingCategory.CODE_SIMPLIFICATION

    @pytest.mark.parametrize(
        ("category", "expected"),
        [
            ("reliability", FindingCategory.BUG),
            ("maintainability", FindingCategory.CODE_SIMPLIFICATION),
        ],
        ids=["reliability-to-bug", "maintainability-to-code-simplification"],
    )
    def test_normalizes_overlapping_categories(self, category: str, expected: FindingCategory) -> None:
        """Test that overlapping category labels normalize to the base taxonomy."""

        finding = parse_finding_line(
            f'{{"path":"a.py","line":3,"side":"RIGHT","category":"{category}","severity":"medium","title":"T","body":"B"}}'
        )

        assert finding is not None
        assert finding.category is expected

    def test_unknown_category_maps_to_other(self) -> None:
        """Test that unknown category text keeps the finding and labels it as other."""

        finding = parse_finding_line(
            '{"path":"a.py","line":3,"side":"RIGHT","category":"surprise","severity":"medium","title":"T","body":"B"}'
        )

        assert finding is not None
        assert finding.category is FindingCategory.OTHER

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

    @pytest.mark.parametrize(
        "blob",
        [
            '{"findings": [{"path":"a.py","line":1,"side":"RIGHT","severity":"high","title":"A","body":"B"}]}',
            '[{"path":"a.py","line":1,"side":"LEFT","severity":"low","title":"A","body":"B"}]',
            '{"findings": []}',
        ],
        ids=["findings-object", "array", "empty-findings-object"],
    )
    def test_raises_on_non_jsonl_json_shapes(self, blob: str) -> None:
        """Test that non-JSONL JSON shapes are rejected."""

        with pytest.raises(ReviewBackendError):
            asyncio.run(collect(blob))

    def test_raises_on_object_without_findings_key(self) -> None:
        """Test that a JSON object lacking a findings key raises instead of approving as zero findings."""

        with pytest.raises(ReviewBackendError):
            asyncio.run(collect("{}"))

    def test_blank_output_is_clean(self) -> None:
        """Test that blank model output yields no findings without raising."""

        assert asyncio.run(collect("   \n")) == []

    def test_no_findings_marker_is_clean(self) -> None:
        """Test that an explicit no-findings marker is treated as a clean review even amid narration."""

        output = f"Reviewed the diff per the skill.\nThe review is complete.\n{CONFIG['no_findings_marker']}"

        assert asyncio.run(collect(output)) == []

    def test_inline_marker_mention_still_raises(self) -> None:
        """Test that output merely mentioning the marker inline (not alone on a line) is still unparseable."""

        output = f"I would emit {CONFIG['no_findings_marker']} but the JSON got mangled here."

        with pytest.raises(ReviewBackendError):
            asyncio.run(collect(output))

    def test_marker_does_not_mask_unparsed_findings(self) -> None:
        """Test that a no-findings marker alongside a finding-shaped line that failed to parse still raises."""

        malformed = '{"path":"a.py","line":1,"side":"RIGHT","severity":"bogus","title":"T","body":"B"}'
        output = f"{malformed}\n{CONFIG['no_findings_marker']}"

        with pytest.raises(ReviewBackendError):
            asyncio.run(collect(output))

    def test_raises_on_unparseable_output(self) -> None:
        """Test that non-empty output with no parseable findings raises instead of approving silently."""

        with pytest.raises(ReviewBackendError):
            asyncio.run(collect("This PR looks great, no issues found!\n"))

    def test_unparseable_error_includes_output_snippet(self) -> None:
        """Test that the unparseable-output error carries the offending text so the failure is debuggable."""

        with pytest.raises(ReviewBackendError, match="not json findings"):
            asyncio.run(collect("prose that is not json findings at all\n"))

    def test_raises_on_truncated_final_finding(self) -> None:
        """Test that a stream cut off mid-finding after earlier findings raises instead of dropping it."""

        complete = '{"path":"a.py","line":1,"side":"RIGHT","severity":"high","title":"A","body":"B"}'

        with pytest.raises(ReviewBackendError):
            asyncio.run(collect(f'{complete}\n{{"path":"b.py","line":2,"sev'))

    def test_trailing_prose_after_findings_is_clean(self) -> None:
        """Test that non-JSON trailing text after a finding is ignored rather than treated as truncation."""

        complete = '{"path":"a.py","line":1,"side":"RIGHT","severity":"high","title":"A","body":"B"}'

        findings = asyncio.run(collect(f"{complete}\nDone."))

        assert [finding.title for finding in findings] == ["A"]


class TestCaptureFlushMarker:
    """Test that the flush-marker tee records completion while passing chunks through unchanged."""

    def test_captures_the_completion_marker_line(self) -> None:
        """Test that a completion-marker line sets the holder and the chunks pass through unchanged."""

        marker = CONFIG["flush_complete_marker"]
        chunks, completion = asyncio.run(capture("NO_FINDINGS\n", f"{marker}\n"))

        assert chunks == ["NO_FINDINGS\n", f"{marker}\n"]
        assert completion.complete is True

    def test_captures_a_marker_split_across_chunks(self) -> None:
        """Test that a marker arriving in two chunks is still recognized."""

        marker = CONFIG["flush_complete_marker"]
        chunks, completion = asyncio.run(capture(marker[:6], f"{marker[6:]}\n"))

        assert "".join(chunks) == f"{marker}\n"
        assert completion.complete is True

    def test_captures_a_marker_without_a_trailing_newline(self) -> None:
        """Test that a marker ending the stream without a newline is still recognized."""

        _, completion = asyncio.run(capture("NO_FINDINGS\n", CONFIG["flush_complete_marker"]))

        assert completion.complete is True

    @pytest.mark.parametrize(
        "text",
        ["NO_FINDINGS\n", "REVIEW_PARTIAL\n", "prose mentioning REVIEW_COMPLETE inline\n"],
        ids=["no-findings", "partial", "inline-mention"],
    )
    def test_does_not_capture_without_a_marker_line(self, text: str) -> None:
        """Test that replies without a standalone completion-marker line leave the holder unset."""

        _, completion = asyncio.run(capture(text))

        assert completion.complete is False
