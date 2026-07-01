import asyncio
from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest

from code_review.config import SETTINGS
from code_review.models.shared.github_event import GithubEvent
from code_review_server.models.jobs import ReviewJob
from code_review_server.services.worker import ReviewWorker


class TestRunJob:
    """Test that a job mints an installation token, applies it to the engine, and runs the review."""

    def test_mints_token_and_reviews_under_it(self, monkeypatch) -> None:
        """Test that run_job mints the installation token, sets it on the engine, and invokes the review."""

        monkeypatch.setattr(
            "code_review_server.services.worker.mint_installation_token", AsyncMock(return_value="ghs_job")
        )
        review = AsyncMock(return_value=0)
        monkeypatch.setattr("code_review_server.services.worker.review_event", review)
        monkeypatch.setattr(SETTINGS, "github_token", "")

        job = ReviewJob(
            event_name="pull_request",
            event=GithubEvent(action="opened"),
            repo="octo/repo",
            installation_id=42,
        )
        asyncio.run(ReviewWorker().run_job(job))

        assert SETTINGS.github_token == "ghs_job"

        review.assert_awaited_once_with("pull_request", job.event, "octo/repo", install_signal_handlers=False)

    def test_raises_when_review_returns_failure(self, monkeypatch) -> None:
        """Test that non-zero review results are logged by the consumer as failed jobs."""

        monkeypatch.setattr(
            "code_review_server.services.worker.mint_installation_token", AsyncMock(return_value="ghs_job")
        )
        monkeypatch.setattr("code_review_server.services.worker.review_event", AsyncMock(return_value=1))

        job = ReviewJob(
            event_name="pull_request",
            event=GithubEvent(action="opened"),
            repo="octo/repo",
            installation_id=42,
        )

        async def scenario() -> None:
            await ReviewWorker().run_job(job)

        with pytest.raises(RuntimeError, match="Review job returned exit code 1"):
            asyncio.run(scenario())


class TestStop:
    """Test that shutdown drains already-accepted jobs before cancelling the consumer."""

    def test_drains_queued_jobs_before_stopping(self, monkeypatch) -> None:
        """Test that stop processes an accepted job instead of dropping it on shutdown."""

        worker = ReviewWorker()
        processed: list[ReviewJob] = []

        async def fake_run_job(job: ReviewJob) -> None:
            processed.append(job)

        monkeypatch.setattr(worker, "run_job", fake_run_job)
        job = ReviewJob(
            event_name="pull_request",
            event=GithubEvent(action="opened"),
            repo="octo/repo",
            installation_id=42,
        )

        async def scenario() -> None:
            worker.start()
            await worker.submit(job)
            await worker.stop()

        asyncio.run(scenario())

        assert processed == [job]


class TestConsumeSurvivesFailure:
    """Test that the consumer survives a failing job and still processes the next one."""

    def test_failed_job_does_not_stop_the_consumer(
        self, monkeypatch, mock_server_settings: Callable[..., None]
    ) -> None:
        """Test that a job raising an error is logged and the next job still runs."""

        mock_server_settings(shutdown_drain_seconds=1.0)
        worker = ReviewWorker()
        processed: list[ReviewJob] = []

        async def fake_run_job(job: ReviewJob) -> None:
            if job.repo == "octo/boom":
                raise RuntimeError("review blew up")

            processed.append(job)

        monkeypatch.setattr(worker, "run_job", fake_run_job)
        failing = ReviewJob(
            event_name="pull_request",
            event=GithubEvent(action="opened"),
            repo="octo/boom",
            installation_id=1,
        )
        succeeding = ReviewJob(
            event_name="pull_request",
            event=GithubEvent(action="opened"),
            repo="octo/ok",
            installation_id=2,
        )

        async def scenario() -> None:
            worker.start()
            await worker.submit(failing)
            await worker.submit(succeeding)
            await worker.stop()

        asyncio.run(scenario())

        assert processed == [succeeding]
