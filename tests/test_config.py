import pytest

from code_review.config import ReviewModel, Settings, parse_bool, split_list
from code_review.models.shared.severity import Severity


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


class TestPrReviewSummarySetting:
    """Test that the pr-review-summary input parses from the environment and defaults on."""

    def test_defaults_true(self, monkeypatch) -> None:
        """Test that an unset input leaves the summary enabled."""

        monkeypatch.delenv("PR_REVIEW_SUMMARY", raising=False)

        assert Settings().pr_review_summary is True

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("false", False), ("true", True)],
        ids=["disabled", "enabled"],
    )
    def test_parses_env(self, monkeypatch, raw: str, expected: bool) -> None:
        """Test that the string input parses to a boolean."""

        monkeypatch.setenv("PR_REVIEW_SUMMARY", raw)

        assert Settings().pr_review_summary is expected


class TestEnforceProjectRulesSetting:
    """Test that the enforce-project-rules input parses from the environment and defaults on."""

    def test_defaults_true(self, monkeypatch) -> None:
        """Test that an unset input leaves rule enforcement enabled."""

        monkeypatch.delenv("ENFORCE_PROJECT_RULES", raising=False)

        assert Settings().enforce_project_rules is True

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("false", False), ("true", True)],
        ids=["disabled", "enabled"],
    )
    def test_parses_env(self, monkeypatch, raw: str, expected: bool) -> None:
        """Test that the string input parses to a boolean."""

        monkeypatch.setenv("ENFORCE_PROJECT_RULES", raw)

        assert Settings().enforce_project_rules is expected


class TestSimplifySettings:
    """Test that the simplify-* inputs parse from the environment and default off."""

    @pytest.mark.parametrize(
        "env_name",
        ["SIMPLIFY_SUGGEST", "SIMPLIFY_NEARBY_CODE"],
        ids=["suggest", "nearby-code"],
    )
    def test_defaults_false(self, monkeypatch, env_name: str) -> None:
        """Test that an unset input leaves the option disabled."""

        monkeypatch.delenv(env_name, raising=False)
        settings = Settings()

        assert getattr(settings, env_name.lower()) is False

    @pytest.mark.parametrize(
        "env_name",
        ["SIMPLIFY_SUGGEST", "SIMPLIFY_NEARBY_CODE"],
        ids=["suggest", "nearby-code"],
    )
    def test_parses_true(self, monkeypatch, env_name: str) -> None:
        """Test that the string input parses to a boolean."""

        monkeypatch.setenv(env_name, "true")
        settings = Settings()

        assert getattr(settings, env_name.lower()) is True


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
