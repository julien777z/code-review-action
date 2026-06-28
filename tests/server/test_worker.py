import asyncio
from unittest.mock import AsyncMock

from code_review.config import SETTINGS
from code_review.models.shared.github_event import GithubEvent
from code_review_server import worker as worker_module
from code_review_server.worker import ReviewJob, ReviewWorker


class TestRunJob:
    """Test that a job mints an installation token, applies it to the engine, and runs the review."""

    def test_mints_token_and_reviews_under_it(self, monkeypatch) -> None:
        """Test that run_job mints the installation token, sets it on SETTINGS, and invokes review_event."""

        monkeypatch.setattr(worker_module, "mint_installation_token", AsyncMock(return_value="ghs_job"))
        review = AsyncMock(return_value=0)
        monkeypatch.setattr(worker_module, "review_event", review)
        monkeypatch.setattr(SETTINGS, "github_token", "")

        job = ReviewJob(
            event_name="pull_request",
            event=GithubEvent(action="opened"),
            repo="octo/repo",
            installation_id=42,
        )
        asyncio.run(ReviewWorker().run_job(job))

        assert SETTINGS.github_token == "ghs_job"

        review.assert_awaited_once_with("pull_request", job.event, "octo/repo")


class TestStop:
    """Test that shutdown drains already-accepted jobs before cancelling the consumer."""

    def test_drains_queued_jobs_before_stopping(self, monkeypatch) -> None:
        """Test that stop processes an accepted job instead of dropping it on shutdown."""

        review_worker = ReviewWorker()
        processed: list[ReviewJob] = []

        async def fake_run_job(job: ReviewJob) -> None:
            processed.append(job)

        monkeypatch.setattr(review_worker, "run_job", fake_run_job)
        job = ReviewJob(
            event_name="pull_request",
            event=GithubEvent(action="opened"),
            repo="octo/repo",
            installation_id=42,
        )

        async def scenario() -> None:
            review_worker.start()
            await review_worker.submit(job)
            await review_worker.stop()

        asyncio.run(scenario())

        assert processed == [job]
