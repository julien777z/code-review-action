from pathlib import Path

from code_review.prompt import SKILL_RELATIVE

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestImageBundlesSkill:
    """Test that the Docker image ships the code-review skill the engine loads at runtime."""

    def test_skill_file_exists_to_copy(self) -> None:
        """Test that the skill the engine loads is present in the repo to copy into the image."""

        assert (REPO_ROOT / SKILL_RELATIVE).is_file()

    def test_dockerfile_copies_the_skill(self) -> None:
        """Test that the Dockerfile copies the skill directory into the image."""

        skill_dir = Path(SKILL_RELATIVE).parent.as_posix()

        assert skill_dir in (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    def test_dockerignore_keeps_the_skill_in_context(self) -> None:
        """Test that .dockerignore does not exclude the skill tree from the build context."""

        top_dir = Path(SKILL_RELATIVE).parts[0]
        ignore_lines = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

        assert top_dir not in ignore_lines
