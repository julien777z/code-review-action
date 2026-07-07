from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_action() -> dict:
    """Load the composite action definition."""

    return yaml.safe_load((REPO_ROOT / "action.yml").read_text(encoding="utf-8"))


def review_step(action: dict) -> dict:
    """Return the review step from the composite action."""

    return next(step for step in action["runs"]["steps"] if step["name"] == "Run code review")


class TestCompositeAction:
    """Test the process wiring that lets the action clean up an in-progress verdict check."""

    def test_review_process_replaces_shell(self) -> None:
        """Test that cancellation reaches Python instead of stopping at the Bash wrapper."""

        assert "exec python -m code_review" in review_step(load_action())["run"]


class TestPrReviewSummaryInput:
    """Test that the pr-review-summary input is wired through to the runner environment."""

    def test_input_defaults_true(self) -> None:
        """Test that the input exists and defaults to enabled."""

        assert load_action()["inputs"]["pr-review-summary"]["default"] == "true"

    def test_env_carries_the_input(self) -> None:
        """Test that the review step passes the input as the PR_REVIEW_SUMMARY env var."""

        assert review_step(load_action())["env"]["PR_REVIEW_SUMMARY"] == "${{ inputs.pr-review-summary }}"


class TestRoutineInputsRemoved:
    """Test that the retired Claude routine inputs are no longer declared."""

    @pytest.mark.parametrize(
        "removed",
        ["claude-mode", "claude-routine-api-key", "claude-routine-id", "claude-routine-url"],
        ids=["mode", "routine-key", "routine-id", "routine-url"],
    )
    def test_input_absent(self, removed: str) -> None:
        """Test that a retired routine input is not present in the action."""

        assert removed not in load_action()["inputs"]
