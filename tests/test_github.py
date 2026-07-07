import asyncio
import subprocess
from collections.abc import Awaitable, Callable

import pytest

from code_review.github import (
    already_reviewed,
    head_check_concluded,
    is_diff_too_large,
    pull_request_body,
    resolve_threads,
    update_pull_request_body,
)


def run_gh_recorder(tokens: list[str | None]) -> Callable[..., Awaitable[str]]:
    """Build a run_gh double that records the token each call is given."""

    async def _run_gh(args: list[str], stdin: str | None = None, token: str | None = None) -> str:
        tokens.append(token)

        return ""

    return _run_gh


def run_gh_call_recorder(calls: list[tuple[list[str], str | None]]) -> Callable[..., Awaitable[str]]:
    """Build a run_gh double that records the args and stdin each call is given."""

    async def _run_gh(args: list[str], stdin: str | None = None, token: str | None = None) -> str:
        calls.append((args, stdin))

        return ""

    return _run_gh


def run_gh_returning(value: str) -> Callable[..., Awaitable[str]]:
    """Build a run_gh double that returns a fixed stdout string."""

    async def _run_gh(args: list[str], stdin: str | None = None, token: str | None = None) -> str:
        return value

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


class TestIsDiffTooLarge:
    """Test detection of oversized-diff failures from gh pr diff."""

    @pytest.mark.parametrize(
        "stderr",
        [
            "gh: Not Acceptable (HTTP 406)",
            "the diff exceeded the maximum number of files",
            "this diff is too large to display",
        ],
        ids=["not-acceptable", "exceeded", "too-large"],
    )
    def test_detects_oversized(self, stderr: str) -> None:
        """Test that oversized-diff failures are recognized."""

        exc = subprocess.CalledProcessError(1, ["gh", "pr", "diff"], stderr=stderr)

        assert is_diff_too_large(exc) is True

    def test_ignores_unrelated_error(self) -> None:
        """Test that an unrelated failure is not treated as an oversized diff."""

        exc = subprocess.CalledProcessError(1, ["gh", "pr", "diff"], stderr="gh: Not Found (HTTP 404)")

        assert is_diff_too_large(exc) is False


class TestPullRequestBody:
    """Test that the PR body is read, coercing a null body to an empty string."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [('{"body": "Hello"}', "Hello"), ('{"body": null}', ""), ('{"body": ""}', "")],
        ids=["text", "null", "empty"],
    )
    def test_reads_body(self, monkeypatch, mock_config, raw: str, expected: str) -> None:
        """Test that a null or empty body resolves to an empty string."""

        monkeypatch.setattr("code_review.github.run_gh", run_gh_returning(raw))

        assert asyncio.run(pull_request_body("octo/repo", 7)) == expected


class TestUpdatePullRequestBody:
    """Test that updating the PR body issues a PATCH with the new body as JSON stdin."""

    def test_patches_with_body_payload(self, monkeypatch, mock_config) -> None:
        """Test that the PATCH targets the pulls endpoint and sends the body as JSON."""

        calls: list[tuple[list[str], str | None]] = []
        monkeypatch.setattr("code_review.github.run_gh", run_gh_call_recorder(calls))

        asyncio.run(update_pull_request_body("octo/repo", 7, "New body"))
        args, stdin = calls[0]

        assert args == ["api", "--method", "PATCH", "repos/octo/repo/pulls/7", "--input", "-"]
        assert stdin == '{"body":"New body"}'


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
