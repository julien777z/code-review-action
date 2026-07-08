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


class TestBooleanFeatureInputs:
    """Test that the feature inputs carry their default and are wired to the runner environment."""

    @pytest.mark.parametrize(
        ("input_name", "env_name", "default"),
        [
            ("pr-review-summary", "PR_REVIEW_SUMMARY", "true"),
            ("enforce-project-rules", "ENFORCE_PROJECT_RULES", "true"),
            ("simplify-suggest", "SIMPLIFY_SUGGEST", "false"),
            ("simplify-nearby-code", "SIMPLIFY_NEARBY_CODE", "false"),
        ],
        ids=["pr-review-summary", "enforce-project-rules", "simplify-suggest", "simplify-nearby-code"],
    )
    def test_input_default_and_env_wired(self, input_name: str, env_name: str, default: str) -> None:
        """Test that the input carries its default and passes through as its env var."""

        action = load_action()

        assert action["inputs"][input_name]["default"] == default
        assert review_step(action)["env"][env_name] == "${{ inputs.%s }}" % input_name


class TestStringInputs:
    """Test that the free-form string inputs are declared and wired to the runner environment."""

    @pytest.mark.parametrize(
        ("input_name", "env_name"),
        [
            ("project-rules-severity", "PROJECT_RULES_SEVERITY"),
            ("simplify-suggest-severity", "SIMPLIFY_SUGGEST_SEVERITY"),
        ],
        ids=["project-rules-severity", "simplify-suggest-severity"],
    )
    def test_input_default_and_env_wired(self, input_name: str, env_name: str) -> None:
        """Test that the input defaults to empty and passes through as its env var."""

        action = load_action()

        assert action["inputs"][input_name]["default"] == ""
        assert review_step(action)["env"][env_name] == "${{ inputs.%s }}" % input_name


class TestClaudeInputDescriptions:
    """Test that Claude input descriptions match the current Managed Agents backend."""

    def test_descriptions_do_not_reference_retired_api_backend(self) -> None:
        """Test that Claude backend descriptions no longer point operators at the old API-only mode."""

        inputs = load_action()["inputs"]

        assert "API backend" not in inputs["anthropic-api-key"]["description"]
        assert "API backend" not in inputs["claude-model"]["description"]
        assert "Managed Agents" in inputs["claude-model"]["description"]


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
