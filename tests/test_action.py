from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestCompositeAction:
    """Test the process wiring that lets the action clean up an in-progress verdict check."""

    def test_review_process_replaces_shell(self) -> None:
        """Test that cancellation reaches Python instead of stopping at the Bash wrapper."""

        action = yaml.safe_load((REPO_ROOT / "action.yml").read_text(encoding="utf-8"))
        review_step = next(step for step in action["runs"]["steps"] if step["name"] == "Run code review")

        assert "exec python -m code_review" in review_step["run"]
