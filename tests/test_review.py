import asyncio
import subprocess
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest

from code_review.config import CONFIG, DISCLAIMER
from code_review.models.shared.findings import Finding, FindingCategory
from code_review.models.shared.severity import DiffSide, Severity
from code_review.review import (
    REVIEW_BACKEND_ATTEMPTS,
    ReviewBackendError,
    build_inline_comment,
    build_verdict_review,
    cap_decision,
    classify_threads,
    comment_body,
    compute_verdict,
    existing_finding_titles,
    extract_posted_keys,
    finding_anchors,
    finding_kept,
    is_postable,
    is_tier_comment,
    run_review_round,
    stream_findings_with_retry,
    thread_severity,
    thread_title,
    verdict_summary,
)

MARKER = CONFIG["review_marker"]


async def collect(stream: AsyncIterator[Finding]) -> list[Finding]:
    """Drain an async finding stream into a list."""

    return [finding async for finding in stream]


class TestStreamFindingsWithRetry:
    """Test that the streaming backend wrapper retries only before the first finding is produced."""

    def test_retries_before_first_finding(self, flaky_stream_factory, review_inputs_factory, finding_factory) -> None:
        """Test that a retryable failure before any finding is retried until findings stream."""

        finding = finding_factory()
        get_findings, calls = flaky_stream_factory(
            failures=1, error=ReviewBackendError("Bridge request timed out", retryable=True), result=[finding]
        )

        result = asyncio.run(collect(stream_findings_with_retry(get_findings, review_inputs_factory())))

        assert result == [finding]
        assert len(calls) == 2

    def test_no_retry_after_a_finding_is_yielded(
        self, flaky_stream_factory, review_inputs_factory, finding_factory
    ) -> None:
        """Test that a retryable failure after a finding has streamed is not retried."""

        get_findings, calls = flaky_stream_factory(
            failures=1,
            error=ReviewBackendError("dropped mid-stream", retryable=True),
            result=[finding_factory()],
            yield_before_error=True,
        )

        with pytest.raises(ReviewBackendError):
            asyncio.run(collect(stream_findings_with_retry(get_findings, review_inputs_factory())))

        assert len(calls) == 1

    def test_raises_after_exhausting_attempts(self, flaky_stream_factory, review_inputs_factory) -> None:
        """Test that a persistently failing backend gives up after the attempt budget."""

        get_findings, calls = flaky_stream_factory(
            failures=REVIEW_BACKEND_ATTEMPTS, error=ReviewBackendError("timed out", retryable=True)
        )

        with pytest.raises(ReviewBackendError):
            asyncio.run(collect(stream_findings_with_retry(get_findings, review_inputs_factory())))

        assert len(calls) == REVIEW_BACKEND_ATTEMPTS

    def test_non_retryable_error_not_retried(self, flaky_stream_factory, review_inputs_factory) -> None:
        """Test that a non-retryable backend error surfaces immediately without further attempts."""

        get_findings, calls = flaky_stream_factory(
            failures=REVIEW_BACKEND_ATTEMPTS, error=ReviewBackendError("unparseable reply", retryable=False)
        )

        with pytest.raises(ReviewBackendError):
            asyncio.run(collect(stream_findings_with_retry(get_findings, review_inputs_factory())))

        assert len(calls) == 1


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


class TestFindingKept:
    """Test that a finding is kept only when it clears the severity bar and the path filters."""

    @pytest.mark.parametrize(
        ("config_overrides", "finding_overrides", "kept"),
        [
            ({"min_severity": Severity.HIGH}, {"severity": Severity.LOW}, False),
            ({"min_severity": Severity.HIGH}, {"severity": Severity.CRITICAL}, True),
            ({"exclude_paths": ("*.lock",)}, {"path": "poetry.lock"}, False),
            ({"include_paths": ("src/**",)}, {"path": "docs/readme.md"}, False),
            ({"include_paths": ("src/**",)}, {"path": "src/app.py"}, True),
        ],
        ids=["below-min", "meets-min", "excluded", "not-included", "included"],
    )
    def test_finding_kept(self, mock_config, finding_factory, config_overrides, finding_overrides, kept) -> None:
        """Test that severity and path filters decide whether a finding is kept."""

        mock_config(**config_overrides)

        assert finding_kept(finding_factory(**finding_overrides)) is kept


class TestCapDecision:
    """Test that the cap decision honors the running Low and total caps."""

    @pytest.mark.parametrize(
        ("config_overrides", "severity", "low_count", "total_count", "expected"),
        [
            ({"low_findings_cap": 1}, Severity.LOW, 0, 0, True),
            ({"low_findings_cap": 1}, Severity.LOW, 1, 1, False),
            ({"low_findings_cap": 1}, Severity.HIGH, 1, 1, True),
            ({"max_findings": 2}, Severity.HIGH, 0, 2, False),
            ({"max_findings": 2}, Severity.HIGH, 0, 1, True),
        ],
        ids=["low-under-cap", "low-over-cap", "high-ignores-low-cap", "total-cap-hit", "total-cap-under"],
    )
    def test_cap_decision(
        self, mock_config, finding_factory, config_overrides, severity, low_count, total_count, expected
    ) -> None:
        """Test that the running low and total caps decide whether a finding posts."""

        mock_config(**config_overrides)

        assert cap_decision(finding_factory(severity=severity), low_count, total_count) is expected


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
    """Test that runner threads are recognized and their title/severity parsed."""

    @pytest.mark.parametrize(
        "author",
        ["github-actions[bot]", "code-review-app[bot]", "reviewer-pat"],
        ids=["github-actions", "github-app", "pat-user"],
    )
    def test_marked_comment_is_tier_for_any_author(self, thread_comment_factory, author: str) -> None:
        """Test that a comment carrying the marker is recognized as the runner's whatever the author."""

        comment = thread_comment_factory(marker=MARKER, author=author)

        assert is_tier_comment(comment, MARKER) is True

    def test_unmarked_comment_is_not_tier(self, thread_comment_factory) -> None:
        """Test that a comment without the review marker is not recognized as the runner's."""

        comment = thread_comment_factory(body="### Title\n\nA human note.")

        assert is_tier_comment(comment, MARKER) is False

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

class TestExistingFindingTitles:
    """Test that the runner's posted findings are collected per file."""

    def test_collects_runner_threads(self, monkeypatch, review_thread_factory) -> None:
        """Test that only threads carrying the review marker contribute posted findings."""

        threads = [
            review_thread_factory(title="Mine", severity="Critical", marker=MARKER, path="src/app.py"),
            review_thread_factory(title="Human", path="src/app.py", body="### Human\n\nA human note."),
        ]
        monkeypatch.setattr("code_review.review.list_review_threads", AsyncMock(return_value=threads))

        result = asyncio.run(existing_finding_titles("octo/repo", 7, MARKER))

        assert list(result) == ["src/app.py"]
        assert result["src/app.py"][0].title == "Mine"
        assert result["src/app.py"][0].severity == "critical"

    def test_thread_listing_failure_raises(self, monkeypatch) -> None:
        """Test that thread listing failures are not treated as an empty prior-finding set."""

        monkeypatch.setattr(
            "code_review.review.list_review_threads",
            AsyncMock(side_effect=subprocess.CalledProcessError(1, ["gh", "api"])),
        )

        with pytest.raises(subprocess.CalledProcessError):
            asyncio.run(existing_finding_titles("octo/repo", 7, MARKER))


class TestExtractPostedKeys:
    """Test that the runner's already-posted keys are pulled from the review threads."""

    def test_collects_runner_keys(self, review_thread_factory) -> None:
        """Test that only marker-carrying runner threads contribute posted keys."""

        threads = [
            review_thread_factory(title="Mine", path="src/app.py", marker=MARKER),
            review_thread_factory(title="Human", path="src/app.py", body="### Human\n\nA human note."),
        ]

        assert extract_posted_keys(threads, MARKER) == {("src/app.py", "Mine")}


class TestClassifyThreads:
    """Test that gone findings resolve or stay open per the outdated/blocking rules."""

    def test_classifies(self, mock_config, review_thread_factory) -> None:
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

        open_keys, stale_ids, kept_blocking = classify_threads(
            threads, MARKER, {("src/app.py", "Current")}, {"src/app.py"}
        )

        assert ("src/app.py", "Current") in open_keys
        assert stale_ids == ["gone-low"]
        assert ("src/app.py", "GoneCrit") in kept_blocking
        assert ("src/app.py", "GoneCrit") in open_keys


class TestRunReviewRound:
    """Test that the round streams findings, posts each inline, and records the verdict."""

    def test_posts_each_anchorable_finding_inline(
        self, mock_config, review_github_mocks, stream_findings_factory, pull_request_factory, finding_factory
    ) -> None:
        """Test that each anchorable finding is posted as its own inline comment as it streams."""

        review_github_mocks["diff_anchors"].return_value = ({"src/app.py": ({10, 20}, set())}, set())
        findings = [
            finding_factory(path="src/app.py", line=10, title="A"),
            finding_factory(path="src/app.py", line=20, title="B"),
        ]

        result = asyncio.run(run_review_round(pull_request_factory(), MARKER, stream_findings_factory(findings)))

        assert result.exit_code == 0
        assert result.diff == ""
        assert review_github_mocks["post_comment"].await_count == 2

    def test_diff_too_large_posts_note_and_skips(
        self, mock_config, review_github_mocks, stream_findings_factory, pull_request_factory
    ) -> None:
        """Test that an oversized diff posts a note, records a neutral verdict, and returns success."""

        review_github_mocks["pull_request_diff_if_available"].return_value = None

        result = asyncio.run(run_review_round(pull_request_factory(), MARKER, stream_findings_factory([])))

        assert result.exit_code == 0
        review_github_mocks["post_review"].assert_awaited_once()
        body = review_github_mocks["post_review"].await_args.args[2].body

        assert "too large to auto-review" in body
        assert review_github_mocks["complete_check_run"].await_args.args[2] == "neutral"

    def test_non_size_diff_error_propagates(
        self, mock_config, review_github_mocks, stream_findings_factory, pull_request_factory
    ) -> None:
        """Test that a diff fetch failure unrelated to size still fails loudly."""

        review_github_mocks["pull_request_diff_if_available"].side_effect = subprocess.CalledProcessError(
            1, ["gh", "pr", "diff"], stderr="gh: Not Found (HTTP 404)"
        )

        with pytest.raises(subprocess.CalledProcessError):
            asyncio.run(run_review_round(pull_request_factory(), MARKER, stream_findings_factory([])))

    def test_out_of_bounds_goes_to_verdict_body(
        self, mock_config, review_github_mocks, stream_findings_factory, pull_request_factory, finding_factory
    ) -> None:
        """Test that a finding on a too-large unpatched file goes into the verdict review, not an inline comment."""

        review_github_mocks["diff_anchors"].return_value = ({}, {"big.txt"})
        findings = [finding_factory(path="big.txt", line=1, title="Big", severity=Severity.HIGH)]

        asyncio.run(run_review_round(pull_request_factory(), MARKER, stream_findings_factory(findings)))

        assert review_github_mocks["post_comment"].await_count == 0
        assert review_github_mocks["post_review"].await_count == 1

        payload = review_github_mocks["post_review"].await_args.args[2]

        assert payload.comments == []
        assert "Findings not posted inline:" in payload.body

    @pytest.mark.parametrize(
        ("config_overrides", "severities", "expected_posts"),
        [
            ({"low_findings_cap": 1}, [Severity.LOW, Severity.LOW, Severity.HIGH], 2),
            ({"max_findings": 1}, [Severity.HIGH, Severity.HIGH], 1),
        ],
        ids=["low-cap", "total-cap"],
    )
    def test_caps_bound_inline_comments(
        self,
        mock_config,
        review_github_mocks,
        stream_findings_factory,
        pull_request_factory,
        finding_factory,
        config_overrides,
        severities,
        expected_posts,
    ) -> None:
        """Test that the low and total caps bound how many inline comments post."""

        mock_config(**config_overrides)
        lines = list(range(1, len(severities) + 1))
        review_github_mocks["diff_anchors"].return_value = ({"src/app.py": (set(lines), set())}, set())
        findings = [
            finding_factory(path="src/app.py", line=line, title=f"T{line}", severity=severity)
            for line, severity in zip(lines, severities)
        ]

        asyncio.run(run_review_round(pull_request_factory(), MARKER, stream_findings_factory(findings)))

        assert review_github_mocks["post_comment"].await_count == expected_posts

    def test_does_not_repost_already_posted_finding(
        self,
        mock_config,
        review_github_mocks,
        stream_findings_factory,
        pull_request_factory,
        finding_factory,
        review_thread_factory,
    ) -> None:
        """Test that a finding already posted on the PR is not posted again."""

        review_github_mocks["diff_anchors"].return_value = ({"src/app.py": ({10}, set())}, set())
        review_github_mocks["list_review_threads"].return_value = [
            review_thread_factory(title="Off-by-one error", path="src/app.py", marker=MARKER)
        ]
        findings = [finding_factory(path="src/app.py", line=10, title="Off-by-one error")]

        asyncio.run(run_review_round(pull_request_factory(), MARKER, stream_findings_factory(findings)))

        assert review_github_mocks["post_comment"].await_count == 0

    def test_head_advanced_before_review_skips(
        self, mock_config, review_github_mocks, stream_findings_factory, pull_request_factory, finding_factory
    ) -> None:
        """Test that an advanced head before streaming skips the round without posting."""

        review_github_mocks["current_head_sha"].return_value = "moved-sha"
        review_github_mocks["diff_anchors"].return_value = ({"src/app.py": ({10}, set())}, set())
        findings = [finding_factory(path="src/app.py", line=10)]

        result = asyncio.run(run_review_round(pull_request_factory(head_sha="abc123"), MARKER, stream_findings_factory(findings)))

        assert result.exit_code == 0
        assert review_github_mocks["post_comment"].await_count == 0
        assert review_github_mocks["post_review"].await_count == 0
        assert review_github_mocks["complete_check_run"].await_args.args[2] == "cancelled"

    def test_backend_failure_before_post_concludes_failed(
        self, mock_config, review_github_mocks, flaky_stream_factory, pull_request_factory
    ) -> None:
        """Test that a backend failure before any comment concludes the check as failed and posts nothing."""

        get_findings, _ = flaky_stream_factory(
            failures=REVIEW_BACKEND_ATTEMPTS, error=ReviewBackendError("model error", retryable=False)
        )

        result = asyncio.run(run_review_round(pull_request_factory(), MARKER, get_findings))

        assert result.exit_code == 1
        assert review_github_mocks["post_comment"].await_count == 0
        assert review_github_mocks["post_review"].await_count == 0
        assert review_github_mocks["complete_check_run"].await_args.args[2] == "action_required"

    def test_rejected_inline_post_warns_without_verdict_body(
        self, mock_config, review_github_mocks, stream_findings_factory, pull_request_factory, finding_factory
    ) -> None:
        """Test that a finding whose inline post is rejected is not counted or copied into the verdict body."""

        review_github_mocks["post_comment"].return_value = False
        review_github_mocks["diff_anchors"].return_value = ({"src/app.py": ({10}, set())}, set())
        findings = [finding_factory(path="src/app.py", line=10, title="Off-by-one error", severity=Severity.HIGH)]

        result = asyncio.run(run_review_round(pull_request_factory(), MARKER, stream_findings_factory(findings)))

        assert result.exit_code == 0
        assert review_github_mocks["post_comment"].await_count == 1
        assert review_github_mocks["post_review"].await_count == 0
        assert review_github_mocks["complete_check_run"].await_args.args[2] == "success"

    def test_approval_disable_posts_verdict_review_to_record_head(
        self, mock_config, review_github_mocks, stream_findings_factory, pull_request_factory
    ) -> None:
        """Test that approval-disable mode posts the verdict review even with no findings to record the head."""

        mock_config(approval_disable=True)

        asyncio.run(run_review_round(pull_request_factory(), MARKER, stream_findings_factory([])))

        assert review_github_mocks["post_review"].await_count == 1
