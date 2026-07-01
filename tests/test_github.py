import asyncio
import subprocess
from collections.abc import Awaitable, Callable

import pytest

from code_review.github import already_reviewed, head_check_concluded, resolve_threads


def run_gh_recorder(tokens: list[str | None]) -> Callable[..., Awaitable[str]]:
    """Build a run_gh double that records the token each call is given."""

    async def _run_gh(args: list[str], stdin: str | None = None, token: str | None = None) -> str:
        tokens.append(token)

        return ""

    return _run_gh


def run_gh_failing(exc: subprocess.CalledProcessError) -> Callable[..., Awaitable[str]]:
    """Build a run_gh double that raises the given process error."""

    async def _run_gh(args: list[str], stdin: str | None = None, token: str | None = None) -> str:
        raise exc

    return _run_gh


class TestAlreadyReviewed:
    """Test duplicate-review detection behavior."""

    def test_rate_limit_returns_already_reviewed(self, monkeypatch, mock_config) -> None:
        """Test that GitHub rate limits make the duplicate guard skip posting."""

        for stderr in (
            "gh: API rate limit exceeded for installation. (HTTP 403)",
            "gh: Too Many Requests (HTTP 429)",
        ):
            exc = subprocess.CalledProcessError(1, ["gh", "api"], stderr=stderr)
            monkeypatch.setattr("code_review.github.run_gh", run_gh_failing(exc))

            assert asyncio.run(already_reviewed("octo/repo", 7, "abc123", "<!-- marker -->")) is True

    def test_non_rate_limit_error_raises(self, monkeypatch, mock_config) -> None:
        """Test that unrelated GitHub API failures still fail loudly."""

        exc = subprocess.CalledProcessError(1, ["gh", "api"], stderr="gh: Not Found (HTTP 404)")
        monkeypatch.setattr("code_review.github.run_gh", run_gh_failing(exc))

        with pytest.raises(subprocess.CalledProcessError):
            asyncio.run(already_reviewed("octo/repo", 7, "abc123", "<!-- marker -->"))


class TestHeadCheckConcluded:
    """Test completed check-run duplicate guard behavior."""

    def test_rate_limit_returns_concluded(self, monkeypatch, mock_config) -> None:
        """Test that GitHub rate limits make the duplicate guard skip reviewing."""

        exc = subprocess.CalledProcessError(
            1,
            ["gh", "api"],
            output="gh: API rate limit exceeded for installation. (HTTP 403)",
        )
        monkeypatch.setattr("code_review.github.run_gh", run_gh_failing(exc))

        assert asyncio.run(head_check_concluded("octo/repo", "abc123")) is True

    def test_non_rate_limit_error_raises(self, monkeypatch, mock_config) -> None:
        """Test that unrelated GitHub API failures still fail loudly."""

        exc = subprocess.CalledProcessError(1, ["gh", "api"], stderr="gh: Bad credentials (HTTP 401)")
        monkeypatch.setattr("code_review.github.run_gh", run_gh_failing(exc))

        with pytest.raises(subprocess.CalledProcessError):
            asyncio.run(head_check_concluded("octo/repo", "abc123"))


class TestResolveThreads:
    """Test that review-thread resolution uses the elevated resolve token when one is set."""

    def test_uses_resolve_token_when_set(self, monkeypatch, mock_config) -> None:
        """Test that resolve_threads runs the mutation with the configured resolve token."""

        mock_config(github_token="default-token", resolve_token="elevated-token")
        tokens: list[str | None] = []
        monkeypatch.setattr("code_review.github.run_gh", run_gh_recorder(tokens))

        asyncio.run(resolve_threads("octo/repo", ["T1", "T2"]))

        assert tokens == ["elevated-token", "elevated-token"]

    def test_falls_back_to_github_token(self, monkeypatch, mock_config) -> None:
        """Test that resolve_threads falls back to the github token when no resolve token is set."""

        mock_config(github_token="default-token", resolve_token="")
        tokens: list[str | None] = []
        monkeypatch.setattr("code_review.github.run_gh", run_gh_recorder(tokens))

        asyncio.run(resolve_threads("octo/repo", ["T1"]))

        assert tokens == ["default-token"]
