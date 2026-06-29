import asyncio
import logging
from contextlib import suppress
from typing import Final

from code_review.config import SETTINGS
from code_review.runtime import review_event
from code_review_server.core.config import SERVER_SETTINGS
from code_review_server.models.jobs import ReviewJob
from code_review_server.services.github_app import mint_installation_token

logger = logging.getLogger(__name__)


class ReviewWorker:
    """A single-consumer queue that runs one review at a time so the shared engine token stays race-free."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[ReviewJob] | None = None
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Create the job queue and start the background consumer in the running loop."""

        self.queue = asyncio.Queue()
        self.task = asyncio.create_task(self.consume())

    async def stop(self) -> None:
        """Drain accepted jobs within the configured window, then cancel the background consumer."""

        if self.task is None:
            return

        if self.queue is not None:
            # GitHub got a 202 for each accepted job and will not redeliver, so let the consumer finish
            # the queue before cancelling — bounded so a long review cannot hang shutdown indefinitely.
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.queue.join(), SERVER_SETTINGS.shutdown_drain_seconds)

        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task

    async def submit(self, job: ReviewJob) -> None:
        """Enqueue a review job for the single consumer to process."""

        if self.queue is None:
            raise RuntimeError("The review worker has not been started.")

        await self.queue.put(job)

    async def consume(self) -> None:
        """Process queued jobs strictly one at a time, surviving a failure in any single job."""

        queue = self.queue
        if queue is None:
            return

        while True:
            job = await queue.get()

            try:
                await self.run_job(job)
            # A single failing job must not kill the only consumer; log it and take the next one.
            except Exception:  # pylint: disable=broad-exception-caught
                logger.exception("Review job for %s (installation %s) failed", job.repo, job.installation_id)
            finally:
                queue.task_done()

    async def run_job(self, job: ReviewJob) -> None:
        """Mint an installation token for the job and run one review round under the App identity."""

        token = await mint_installation_token(job.installation_id)

        # One consumer means reviews are serialized, so mutating the shared engine token here is race-free.
        SETTINGS.github_token = token

        await review_event(job.event_name, job.event, job.repo)


review_worker: Final[ReviewWorker] = ReviewWorker()
