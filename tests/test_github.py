import asyncio
from collections.abc import Awaitable, Callable

from code_review.github import resolve_threads


def run_gh_recorder(tokens: list[str | None]) -> Callable[..., Awaitable[str]]:
    """Build a run_gh double that records the token each call is given."""

    async def _run_gh(args: list[str], stdin: str | None = None, token: str | None = None) -> str:
        tokens.append(token)

        return ""

    return _run_gh


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
