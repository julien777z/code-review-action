import asyncio
from unittest.mock import AsyncMock

import pytest

from code_review.config import CONFIG, DISCLAIMER
from code_review.models.shared.severity import DiffSide, Severity
from code_review.review import (
    build_review,
    cap_findings,
    comment_body,
    compute_verdict,
    dedupe_findings,
    existing_finding_titles,
    filter_findings,
    finding_anchors,
    is_postable,
    is_tier_comment,
    reconcile_threads,
    thread_severity,
    thread_title,
    verdict_summary,
)

MARKER = CONFIG["review_marker"]


class TestComputeVerdict:
    """Test that the verdict reflects the open-issue state and blocking severities."""

    @pytest.mark.parametrize(
        ("open_count", "open_blocking", "event", "conclusion"),
        [
            (0, False, "APPROVE", "success"),
            (2, True, "REQUEST_CHANGES", "failure"),
            (2, False, "COMMENT", "neutral"),
        ],
        ids=["clean", "blocking", "non-blocking"],
    )
    def test_verdict(self, open_count: int, open_blocking: bool, event: str, conclusion: str) -> None:
        """Test that the event and conclusion match the open-issue state."""

        result_event, result_conclusion, _ = compute_verdict(open_count, open_blocking)

        assert (result_event, result_conclusion) == (event, conclusion)


class TestVerdictSummary:
    """Test that the verdict summary phrases the open-issue count."""

    def test_approve(self) -> None:
        """Test that an approving round summarizes as no unresolved issues."""

        assert verdict_summary("APPROVE", 0, 0) == "No unresolved issues — approving."

    def test_request_changes_mentions_blocking(self) -> None:
        """Test that a request-changes summary mentions the blocking issue and carried count."""

        summary = verdict_summary("REQUEST_CHANGES", 2, 1)

        assert "2 unresolved issues" in summary
        assert "including 1 from a previous review" in summary
        assert "requesting changes" in summary


class TestFilterFindings:
    """Test that findings below the severity bar or outside the path filters are dropped."""

    def test_drops_below_min_severity(self, mock_config, finding_factory) -> None:
        """Test that findings below min-severity are removed."""

        mock_config(min_severity=Severity.HIGH)
        findings = [finding_factory(severity=Severity.LOW), finding_factory(severity=Severity.CRITICAL)]

        kept = filter_findings(findings)

        assert [finding.severity for finding in kept] == [Severity.CRITICAL]

    def test_applies_exclude_globs(self, mock_config, finding_factory) -> None:
        """Test that findings on excluded paths are removed."""

        mock_config(exclude_paths=("*.lock",))
        findings = [finding_factory(path="poetry.lock"), finding_factory(path="src/app.py")]

        kept = filter_findings(findings)

        assert [finding.path for finding in kept] == ["src/app.py"]

    def test_applies_include_globs(self, mock_config, finding_factory) -> None:
        """Test that only findings under the include globs are kept."""

        mock_config(include_paths=("src/**",))
        findings = [finding_factory(path="src/app.py"), finding_factory(path="docs/readme.md")]

        kept = filter_findings(findings)

        assert [finding.path for finding in kept] == ["src/app.py"]


class TestDedupeFindings:
    """Test that repeat findings sharing a path, line, side, and title collapse."""

    def test_dedupes(self, finding_factory) -> None:
        """Test that an exact duplicate finding is dropped."""

        findings = [finding_factory(title="Bug"), finding_factory(title="Bug")]

        assert len(dedupe_findings(findings)) == 1


class TestCapFindings:
    """Test that the Low and total caps bound how many findings post."""

    def test_caps_low(self, mock_config, finding_factory) -> None:
        """Test that low findings are capped while higher severities are kept."""

        mock_config(low_findings_cap=1)
        findings = [
            finding_factory(severity=Severity.LOW, title="a"),
            finding_factory(severity=Severity.LOW, title="b"),
            finding_factory(severity=Severity.HIGH, title="c"),
        ]

        capped = cap_findings(findings)

        assert [finding.title for finding in capped] == ["a", "c"]

    def test_caps_total(self, mock_config, finding_factory) -> None:
        """Test that the total cap limits the number of findings."""

        mock_config(max_findings=1)
        findings = [finding_factory(title="a"), finding_factory(title="b")]

        assert len(cap_findings(findings)) == 1


class TestAnchoring:
    """Test that anchoring and postability follow the diff's available lines."""

    def test_finding_anchors_on_right(self, finding_factory) -> None:
        """Test that a right-side finding anchors when its line is in the new-side set."""

        anchors = {"src/app.py": ({10}, set())}

        assert finding_anchors(finding_factory(line=10, side=DiffSide.RIGHT), anchors) is True

    def test_unanchored_finding_is_postable_when_unpatched(self, finding_factory) -> None:
        """Test that a finding on a too-large unpatched file is still postable."""

        finding = finding_factory(path="big.txt", line=1)

        assert is_postable(finding, {}, {"big.txt"}) is True

    def test_unanchored_finding_not_postable_otherwise(self, finding_factory) -> None:
        """Test that an unanchorable finding on a patched file is not postable."""

        finding = finding_factory(path="src/app.py", line=999)

        assert is_postable(finding, {"src/app.py": ({10}, set())}, set()) is False


class TestBuildReview:
    """Test that the review splits anchorable findings from too-large-file findings."""

    def test_inline_and_summary(self, finding_factory) -> None:
        """Test that anchorable findings become comments and others go to the summary body."""

        anchors = {"src/app.py": ({10}, set())}
        findings = [
            finding_factory(path="src/app.py", line=10, title="Inline"),
            finding_factory(path="big.txt", line=1, title="Big"),
        ]

        payload = build_review("sha1", findings, anchors, "COMMENT", "Found 2 issues.", MARKER)

        assert len(payload.comments) == 1
        assert payload.comments[0].path == "src/app.py"
        assert "On files too large to anchor inline:" in payload.body
        assert CONFIG["security_open"] in payload.body
        assert CONFIG["security_close"] in payload.body
        assert DISCLAIMER in payload.body
        assert payload.body.rstrip().endswith(MARKER)


class TestCommentBody:
    """Test that an inline comment renders the title, capitalized severity, and marker."""

    def test_render(self, finding_factory) -> None:
        """Test that the comment carries the heading, severity line, and runner marker."""

        body = comment_body(finding_factory(title="Leak", severity=Severity.CRITICAL), MARKER)

        assert "### Leak" in body
        assert "**Critical Severity**" in body
        assert CONFIG["security_open"] in body
        assert CONFIG["security_close"] in body
        assert DISCLAIMER in body
        assert body.rstrip().endswith(MARKER)


class TestThreadParsing:
    """Test that runner threads are recognized and their title/severity parsed."""

    def test_is_tier_comment(self, thread_comment_factory) -> None:
        """Test that a bot comment carrying the marker is recognized as this tier's."""

        assert is_tier_comment(thread_comment_factory(marker=MARKER), MARKER) is True

    def test_unmarked_comment_is_not_tier(self, thread_comment_factory) -> None:
        """Test that a comment without the review marker is not recognized as the runner's."""

        comment = thread_comment_factory(body="### Title\n\n**Critical Severity**\n\nA human note.")

        assert is_tier_comment(comment, MARKER) is False

    def test_title_and_severity(self, thread_comment_factory) -> None:
        """Test that the title heading and severity line parse from the comment body."""

        comment = thread_comment_factory(title="Race condition", severity="High")

        assert thread_title(comment) == "Race condition"
        assert thread_severity(comment) is Severity.HIGH


class TestExistingFindingTitles:
    """Test that the runner's posted findings are collected per file."""

    def test_collects_runner_threads(self, monkeypatch, review_thread_factory) -> None:
        """Test that only threads carrying the review marker contribute posted findings."""

        threads = [
            review_thread_factory(title="Mine", severity="Critical", marker=MARKER, path="src/app.py"),
            review_thread_factory(
                title="Human", path="src/app.py", body="### Human\n\n**Low Severity**\n\nA human note."
            ),
        ]
        monkeypatch.setattr("code_review.review.list_review_threads", AsyncMock(return_value=threads))

        result = asyncio.run(existing_finding_titles("octo/repo", 7, MARKER))

        assert list(result) == ["src/app.py"]
        assert result["src/app.py"][0].title == "Mine"
        assert result["src/app.py"][0].severity == "critical"


class TestReconcileThreads:
    """Test that gone findings resolve or stay open per outdated/blocking rules."""

    def test_classifies(self, mock_config, monkeypatch, review_thread_factory) -> None:
        """Test that current threads stay open, non-blocking gone threads go stale, blocking ones stay."""

        mock_config(approval_include=frozenset({Severity.CRITICAL}))
        threads = [
            review_thread_factory(id="current", title="Current", path="src/app.py", marker=MARKER),
            review_thread_factory(
                id="gone-low", title="GoneLow", severity="Medium", path="src/app.py", marker=MARKER
            ),
            review_thread_factory(
                id="gone-critical", title="GoneCrit", severity="Critical", path="src/app.py", marker=MARKER
            ),
        ]
        monkeypatch.setattr("code_review.review.list_review_threads", AsyncMock(return_value=threads))

        posted, open_keys, stale_ids, kept_blocking = asyncio.run(
            reconcile_threads("octo/repo", 7, MARKER, {("src/app.py", "Current")}, {"src/app.py"})
        )

        assert ("src/app.py", "Current") in open_keys
        assert stale_ids == ["gone-low"]
        assert ("src/app.py", "GoneCrit") in kept_blocking
        assert ("src/app.py", "GoneCrit") in open_keys
