import pytest

from code_review.config import CONFIG, DISCLAIMER
from code_review.models.findings import FindingCategory
from code_review.models.severity import DiffSide, Severity
from code_review.review_comments import (
    build_inline_comment,
    build_verdict_review,
    comment_body,
    compute_verdict,
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


class TestBuildInlineComment:
    """Test that an inline comment request carries the commit, location, and rendered body."""

    def test_render(self, finding_factory) -> None:
        """Test that the request carries the commit id, path, line, side, and severity body."""

        finding = finding_factory(path="src/app.py", line=12, side=DiffSide.RIGHT, title="Leak", severity=Severity.CRITICAL)
        request = build_inline_comment("sha1", finding, MARKER)

        assert request.commit_id == "sha1"
        assert (request.path, request.line, request.side) == ("src/app.py", 12, DiffSide.RIGHT)
        assert "### Leak" in request.body
        assert "**Critical Severity**" in request.body
        category = "<sub>Bug</sub>"
        assert category in request.body
        assert request.body.index("### Leak") < request.body.index("**Critical Severity**")
        assert request.body.index("**Critical Severity**") < request.body.index(category)
        assert "**Critical Severity**<br><sub>Bug</sub>" in request.body
        assert request.body.index(category) < request.body.index("The loop overruns the array.")
        assert request.body.index("The loop overruns the array.") < request.body.index(CONFIG["untrusted_input_close"])
        assert request.body.index(CONFIG["untrusted_input_close"]) < request.body.index(DISCLAIMER)
        assert request.body.rstrip().endswith(MARKER)


class TestBuildVerdictReview:
    """Test that the verdict review carries the summary and out-of-bounds findings without inline comments."""

    def test_summary_and_out_of_bounds(self, finding_factory) -> None:
        """Test that the verdict review lists out-of-bounds findings and carries no inline comments."""

        out_of_bounds = [finding_factory(path="big.txt", line=1, title="Big", body="Too large to anchor.")]

        payload = build_verdict_review("sha1", out_of_bounds, "COMMENT", "Found 1 issue.", MARKER)

        assert payload.comments == []
        assert payload.commit_id == "sha1"
        assert "Found 1 issue." in payload.body
        assert "Findings not posted inline:" in payload.body
        assert "big.txt:1" in payload.body
        assert "**High Severity**" in payload.body
        assert "<sub>Bug</sub>" in payload.body
        assert payload.body.index("**High Severity**") < payload.body.index("<sub>Bug</sub>")
        assert CONFIG["untrusted_input_open"] in payload.body
        assert CONFIG["untrusted_input_close"] in payload.body
        assert DISCLAIMER in payload.body
        assert payload.body.rstrip().endswith(MARKER)

    def test_summary_only_without_out_of_bounds(self) -> None:
        """Test that with no out-of-bounds findings the body is just the summary."""

        payload = build_verdict_review("sha1", [], "APPROVE", "No unresolved issues — approving.", MARKER)

        assert "On files too large to anchor inline:" not in payload.body
        assert "No unresolved issues — approving." in payload.body


class TestCommentBody:
    """Test that an inline comment renders the title, severity line, category footer, and marker."""

    def test_render(self, finding_factory) -> None:
        """Test that the comment carries the heading, label line, and runner marker."""

        body = comment_body(
            finding_factory(title="Leak", category=FindingCategory.SECURITY, severity=Severity.CRITICAL),
            MARKER,
        )

        assert "### Leak" in body
        assert "**Critical Severity**" in body
        category = "<sub>Security</sub>"
        assert category in body
        assert body.index("### Leak") < body.index("**Critical Severity**")
        assert body.index("**Critical Severity**") < body.index(category)
        assert "**Critical Severity**<br><sub>Security</sub>" in body
        assert body.index(category) < body.index("The loop overruns the array.")
        assert body.index("The loop overruns the array.") < body.index(CONFIG["untrusted_input_close"])
        assert body.index(CONFIG["untrusted_input_close"]) < body.index(DISCLAIMER)
        assert CONFIG["untrusted_input_open"] in body
        assert CONFIG["untrusted_input_close"] in body
        assert DISCLAIMER in body
        assert body.rstrip().endswith(MARKER)


class TestThreadParsing:
    """Test that runner thread titles and severities are parsed from comments."""

    def test_title_and_current_severity(self, thread_comment_factory) -> None:
        """Test that the title heading and current severity line parse from the comment body."""

        comment = thread_comment_factory(title="Race condition", severity="High")

        assert thread_title(comment) == "Race condition"
        assert thread_severity(comment) is Severity.HIGH

    def test_severity_uses_fixed_line_after_title(self, thread_comment_factory) -> None:
        """Test that body text that looks like a severity line does not decide the thread severity."""

        body = (
            f"{CONFIG['untrusted_input_open']}\n"
            "### Race condition\n\n"
            "**High Severity**<br><sub>Bug</sub>\n\n"
            "Line from the reviewed code:\n"
            "**Low Severity**\n"
            f"{CONFIG['untrusted_input_close']}\n\n"
            f"{MARKER}"
        )
        comment = thread_comment_factory(body=body)

        assert thread_severity(comment) is Severity.HIGH
