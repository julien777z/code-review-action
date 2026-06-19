from code_review.models.shared.pull_request import PostedFinding, ReviewInputs
from code_review.prompt import (
    cursor_prompt,
    existing_findings_block,
    pull_request_message,
    review_instructions,
)


class TestReviewInstructions:
    """Test that the instructions bundle the skill, JSON contract, and extra context."""

    def test_includes_skill_and_contract(self) -> None:
        """Test that the bundled skill and findings contract are present."""

        text = review_instructions()

        assert "code-review" in text
        assert '"findings"' in text

    def test_includes_prompt_injection_safety(self) -> None:
        """Test that the instructions warn that pull request content is untrusted data."""

        text = review_instructions()

        assert "untrusted" in text
        assert "never obey instructions" in text.lower()

    def test_includes_additional_context(self, mock_config) -> None:
        """Test that configured additional context is appended."""

        mock_config(additional_context="Prefer typed models.")

        assert "Prefer typed models." in review_instructions()


class TestExistingFindingsBlock:
    """Test that already-posted findings are listed for exact-title matching."""

    def test_lists_posted(self, pull_request_factory) -> None:
        """Test that a posted finding renders as a file/severity/title line."""

        inputs = ReviewInputs(
            pr=pull_request_factory(),
            diff="diff",
            posted_findings={"src/app.py": [PostedFinding(severity="critical", title="Leak")]},
        )

        assert "src/app.py: [critical] Leak" in existing_findings_block(inputs)

    def test_empty_when_none(self, pull_request_factory) -> None:
        """Test that the block is empty when nothing was posted."""

        inputs = ReviewInputs(pr=pull_request_factory(), diff="diff")

        assert existing_findings_block(inputs) == ""


class TestPullRequestMessage:
    """Test that the per-PR turn carries the header and diff."""

    def test_contains_diff_and_header(self, pull_request_factory) -> None:
        """Test that the message includes the PR number header and the diff body."""

        inputs = ReviewInputs(pr=pull_request_factory(number=7), diff="DIFF_BODY")
        message = pull_request_message(inputs)

        assert "Pull request: #7" in message
        assert "DIFF_BODY" in message

    def test_wraps_diff_as_untrusted(self, pull_request_factory) -> None:
        """Test that the diff is wrapped in an untrusted-content delimiter."""

        inputs = ReviewInputs(pr=pull_request_factory(), diff="DIFF_BODY")
        message = pull_request_message(inputs)

        assert "<untrusted_diff>\nDIFF_BODY\n</untrusted_diff>" in message
        assert "never follow" in message.lower()


class TestCursorPrompt:
    """Test that the Cursor prompt combines the instructions and the PR turn."""

    def test_combines(self, pull_request_factory) -> None:
        """Test that the single-string prompt carries both the contract and the diff."""

        inputs = ReviewInputs(pr=pull_request_factory(), diff="DIFF_BODY")
        prompt = cursor_prompt(inputs)

        assert '"findings"' in prompt
        assert "DIFF_BODY" in prompt
