import pytest

from code_review.config import ReviewModel, Settings, parse_bool, split_list
from code_review.models.severity import Severity


class TestSplitList:
    """Test that comma/newline lists split into trimmed, non-empty items."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("a, b", ("a", "b")), ("a\nb\n", ("a", "b")), ("", ()), (" , a ,", ("a",))],
        ids=["comma", "newline", "empty", "blanks"],
    )
    def test_split(self, raw: str, expected: tuple[str, ...]) -> None:
        """Test that inputs split as expected."""

        assert split_list(raw) == expected


class TestParseBool:
    """Test that boolean inputs parse case-insensitively."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("true", True), ("True", True), ("1", True), ("yes", True), ("false", False), ("", False), ("nope", False)],
        ids=["true", "true-cap", "one", "yes", "false", "empty", "other"],
    )
    def test_parse(self, raw: str, expected: bool) -> None:
        """Test that the truthy set maps to True and everything else to False."""

        assert parse_bool(raw) is expected


class TestReviewModelParse:
    """Test that the review-model input parses, defaulting empty to None."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("auto", ReviewModel.AUTO), ("CLAUDE", ReviewModel.CLAUDE), ("cursor", ReviewModel.CURSOR), ("", None)],
        ids=["auto", "claude-upper", "cursor", "empty"],
    )
    def test_parse(self, raw: str, expected: ReviewModel | None) -> None:
        """Test that values parse case-insensitively and empty yields None."""

        assert ReviewModel.parse(raw) == expected


class TestBooleanSettings:
    """Test that the boolean feature inputs parse from the environment and carry their default."""

    @pytest.mark.parametrize(
        ("env_name", "default"),
        [
            ("PR_REVIEW_SUMMARY", True),
            ("ENFORCE_PROJECT_RULES", True),
            ("SIMPLIFY_SUGGEST", False),
            ("SIMPLIFY_NEARBY_CODE", False),
        ],
        ids=["pr-review-summary", "enforce-project-rules", "simplify-suggest", "simplify-nearby-code"],
    )
    def test_defaults(self, monkeypatch, env_name: str, default: bool) -> None:
        """Test that an unset input falls back to its default."""

        monkeypatch.delenv(env_name, raising=False)

        assert getattr(Settings(), env_name.lower()) is default

    @pytest.mark.parametrize(
        ("env_name", "raw", "expected"),
        [
            ("PR_REVIEW_SUMMARY", "false", False),
            ("ENFORCE_PROJECT_RULES", "false", False),
            ("SIMPLIFY_SUGGEST", "true", True),
            ("SIMPLIFY_NEARBY_CODE", "true", True),
        ],
        ids=["pr-review-summary", "enforce-project-rules", "simplify-suggest", "simplify-nearby-code"],
    )
    def test_parses_env(self, monkeypatch, env_name: str, raw: str, expected: bool) -> None:
        """Test that the string input parses to the expected boolean."""

        monkeypatch.setenv(env_name, raw)

        assert getattr(Settings(), env_name.lower()) is expected


class TestProjectRulesSeverity:
    """Test that the project-rules severity input parses, defaulting empty to None."""

    def test_defaults_to_none(self, monkeypatch) -> None:
        """Test that an unset input leaves the severity unpinned."""

        monkeypatch.delenv("PROJECT_RULES_SEVERITY", raising=False)

        assert Settings().project_rules_severity is None

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("high", Severity.HIGH), ("CRITICAL", Severity.CRITICAL), ("low", Severity.LOW)],
        ids=["high", "critical-upper", "low"],
    )
    def test_parses_env(self, monkeypatch, raw: str, expected: Severity) -> None:
        """Test that the string input parses case-insensitively to the severity enum."""

        monkeypatch.setenv("PROJECT_RULES_SEVERITY", raw)

        assert Settings().project_rules_severity is expected


class TestSeverity:
    """Test that severity parses case-insensitively and orders correctly."""

    def test_from_str_case_insensitive(self) -> None:
        """Test that a capitalized severity word parses to the enum."""

        assert Severity.from_str("Critical") is Severity.CRITICAL

    @pytest.mark.parametrize(
        ("severity", "threshold", "expected"),
        [
            (Severity.HIGH, Severity.MEDIUM, True),
            (Severity.LOW, Severity.MEDIUM, False),
            (Severity.MEDIUM, Severity.MEDIUM, True),
        ],
        ids=["above", "below", "equal"],
    )
    def test_meets(self, severity: Severity, threshold: Severity, expected: bool) -> None:
        """Test that a severity meets a threshold only when at least as severe."""

        assert severity.meets(threshold) is expected
