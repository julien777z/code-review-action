import pytest

from code_review.config import ReviewModel, parse_bool, resolve_routine_id, split_list
from code_review.models.shared.severity import Severity

ROUTINE_URL = "https://api.anthropic.com/v1/claude_code/routines/rtn_abc/fire"


class TestResolveRoutineId:
    """Test that the routine id resolves from an id or url and rejects both."""

    def test_returns_id_when_only_id_given(self) -> None:
        """Test that an explicit id is returned unchanged."""

        assert resolve_routine_id("rtn_123", "") == "rtn_123"

    def test_parses_id_from_url(self) -> None:
        """Test that the id is parsed out of a fire url."""

        assert resolve_routine_id("", ROUTINE_URL) == "rtn_abc"

    def test_returns_none_when_neither_given(self) -> None:
        """Test that an absent id and url resolve to None."""

        assert resolve_routine_id("", "") is None

    def test_rejects_both(self) -> None:
        """Test that providing both an id and a url raises."""

        with pytest.raises(ValueError):
            resolve_routine_id("rtn_123", ROUTINE_URL)

    def test_rejects_malformed_url(self) -> None:
        """Test that a url without a routine id raises."""

        with pytest.raises(ValueError):
            resolve_routine_id("", "https://example.com/nope")


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
