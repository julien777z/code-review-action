from code_review.config import DISCLAIMER
from code_review.models.shared.severity import Severity
from code_review.review_backends.claude import build_routine_text


class TestBuildRoutineText:
    """Test that the routine fire text carries PR context, policy, and extra context."""

    def test_includes_context_and_policy(self, mock_config, pull_request_factory) -> None:
        """Test that the text names the PR, the blocking severities, and the extra context."""

        mock_config(approval_include=frozenset({Severity.CRITICAL}), additional_context="Focus on auth.")
        text = build_routine_text(pull_request_factory(number=7))

        assert "#7" in text
        assert "critical" in text
        assert "Focus on auth." in text

    def test_approval_disabled(self, mock_config, pull_request_factory) -> None:
        """Test that disabling approval asks the routine for comments only."""

        mock_config(approval_disable=True)
        text = build_routine_text(pull_request_factory())

        assert "comments only" in text.lower()

    def test_includes_safety_and_disclaimer(self, mock_config, pull_request_factory) -> None:
        """Test that the text marks PR content as untrusted and asks for the AI disclaimer."""

        mock_config()
        text = build_routine_text(pull_request_factory())

        assert "untrusted" in text
        assert DISCLAIMER in text
