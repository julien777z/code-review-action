import pytest

from code_review.models.shared.severity import DiffSide, Severity
from code_review.review import ReviewBackendError
from code_review.review_backends.cursor import parse_cursor_reply


class TestParseCursorReply:
    """Test that the Cursor reply parses, normalizes, and rejects bad output."""

    def test_plain_object(self) -> None:
        """Test that a plain findings object parses with a normalized severity."""

        text = '{"findings":[{"path":"a.py","line":3,"side":"RIGHT","severity":"high","title":"T","body":"B"}]}'
        findings = parse_cursor_reply(text)

        assert len(findings) == 1
        assert findings[0].severity is Severity.HIGH

    def test_fenced_code_block(self) -> None:
        """Test that a fenced JSON block parses and the side normalizes."""

        text = '```json\n{"findings":[{"path":"a.py","line":3,"side":"LEFT","severity":"low","title":"T","body":"B"}]}\n```'

        assert parse_cursor_reply(text)[0].side is DiffSide.LEFT

    def test_list_root(self) -> None:
        """Test that a bare findings list parses."""

        text = '[{"path":"a.py","line":3,"side":"RIGHT","severity":"medium","title":"T","body":"B"}]'

        assert len(parse_cursor_reply(text)) == 1

    def test_capitalized_severity_normalized(self) -> None:
        """Test that a capitalized severity word is normalized to the enum."""

        text = '{"findings":[{"path":"a.py","line":1,"side":"RIGHT","severity":"Critical","title":"T","body":"B"}]}'

        assert parse_cursor_reply(text)[0].severity is Severity.CRITICAL

    def test_unknown_severity_skipped(self) -> None:
        """Test that a finding with an unknown severity is skipped rather than crashing."""

        text = '{"findings":[{"path":"a.py","line":1,"side":"RIGHT","severity":"bogus","title":"T","body":"B"}]}'

        assert parse_cursor_reply(text) == []

    def test_empty_findings(self) -> None:
        """Test that an empty findings list yields no findings."""

        assert parse_cursor_reply('{"findings":[]}') == []

    def test_bad_json_raises(self) -> None:
        """Test that unparseable output raises a backend error."""

        with pytest.raises(ReviewBackendError):
            parse_cursor_reply("not json at all")
