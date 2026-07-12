import asyncio
import json
import logging
import os
import shutil
import stat
import tempfile
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final
from pydantic import JsonValue, ValidationError

from code_review.config import SETTINGS
from code_review.errors import ReviewBackendError
from code_review.models.backend import ReviewSessionStreams
from code_review.models.codex import CodexRpcMessage, CodexRpcRequest, CodexThread, CodexTurn
from code_review.models.pull_request import PullRequestContext, ReviewInputs
from code_review.prompt import flush_prompt, pull_request_message, review_instructions

logger = logging.getLogger("code_review.review_backends.codex")

APP_SERVER_LINE_LIMIT: Final[int] = 10 * 1024 * 1024


def mapping(value: JsonValue, label: str) -> dict[str, JsonValue]:
    """Validate that an app-server JSON value is an object."""

    if not isinstance(value, dict):
        raise ReviewBackendError(f"Codex app-server returned an invalid {label} payload.")

    return value


def usage_limit_error(error: dict[str, JsonValue] | None) -> bool:
    """Return whether a Codex turn failure is the structured subscription-limit error."""

    return error is not None and error.get("codexErrorInfo") == "usageLimitExceeded"


class CodexAppServer:
    """A minimal asynchronous client for one Codex app-server subprocess."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.pending: deque[CodexRpcMessage] = deque()
        self.request_id = 0
        self.thread_id: str | None = None
        self.turn_id: str | None = None

    async def send(self, request: CodexRpcRequest) -> None:
        """Write one JSONL request or notification to app-server."""

        if self.process.stdin is None:
            raise ReviewBackendError("Codex app-server stdin is unavailable.", retryable=True)

        payload = request.model_dump_json(exclude_none=True, by_alias=True) + "\n"
        self.process.stdin.write(payload.encode())
        await self.process.stdin.drain()

    async def read(self) -> CodexRpcMessage:
        """Read and validate one app-server response or notification."""

        if self.pending:
            return self.pending.popleft()

        if self.process.stdout is None:
            raise ReviewBackendError("Codex app-server stdout is unavailable.", retryable=True)

        line = await self.process.stdout.readline()
        if not line:
            stderr = ""
            if self.process.stderr is not None:
                stderr = (await self.process.stderr.read()).decode(errors="replace").strip()

            raise ReviewBackendError(
                f"Codex app-server exited unexpectedly: {stderr or 'no diagnostic output'}",
                retryable=True,
            )

        try:
            return CodexRpcMessage.model_validate_json(line)
        except ValidationError as exc:
            raise ReviewBackendError("Codex app-server emitted invalid JSON.") from exc

    async def request(self, method: str, params: dict[str, JsonValue]) -> JsonValue:
        """Send one request and return its matching result while preserving notifications."""

        self.request_id += 1
        request_id = self.request_id
        await self.send(CodexRpcRequest(id=request_id, method=method, params=params))

        deferred: deque[CodexRpcMessage] = deque()
        while True:
            message = await self.read()
            if message.id != request_id:
                deferred.append(message)
                continue

            self.pending.extendleft(reversed(deferred))
            if message.error is not None:
                raise ReviewBackendError(f"Codex app-server request failed: {message.error.message}")

            return message.result

    async def initialize(self, *, reviewing: bool) -> None:
        """Complete the app-server handshake and open an ephemeral agent thread."""

        await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "code_review_action",
                    "title": "Code Review Action",
                    "version": "0.1.0",
                }
            },
        )
        await self.send(CodexRpcRequest(method="initialized"))
        result = mapping(
            await self.request(
                "thread/start",
                {
                    "approvalPolicy": "never",
                    "cwd": os.environ.get("GITHUB_WORKSPACE") or os.getcwd(),
                    "developerInstructions": review_instructions() if reviewing else None,
                    "ephemeral": True,
                    "model": SETTINGS.codex_model,
                    "personality": "none",
                    "sandbox": "read-only",
                },
            ),
            "thread",
        )
        thread = CodexThread.model_validate(mapping(result.get("thread"), "thread"))
        self.thread_id = thread.id

    async def interrupt(self) -> None:
        """Interrupt the active Codex turn and wait until it reaches a terminal state."""

        if self.thread_id is None or self.turn_id is None:
            return

        await self.request(
            "turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id}
        )

        while self.turn_id is not None:
            message = await self.read()
            if message.method != "turn/completed":
                continue

            params = mapping(message.params, "turn completion")
            turn = CodexTurn.model_validate(mapping(params.get("turn"), "turn"))
            if turn.id == self.turn_id:
                self.turn_id = None

    async def turn_text(self, prompt: str, *, interrupt: bool = False) -> AsyncIterator[str]:
        """Start one Codex turn and stream its assistant text deltas."""

        if interrupt:
            await self.interrupt()

        if self.thread_id is None:
            raise ReviewBackendError("Codex app-server thread was not initialized.")

        result = mapping(
            await self.request(
                "turn/start",
                {
                    "effort": "high",
                    "input": [{"type": "text", "text": prompt}],
                    "model": SETTINGS.codex_model,
                    "threadId": self.thread_id,
                },
            ),
            "turn",
        )
        turn = CodexTurn.model_validate(mapping(result.get("turn"), "turn"))
        self.turn_id = turn.id

        while self.turn_id is not None:
            message = await self.read()
            if message.method == "item/agentMessage/delta":
                params = mapping(message.params, "agent message")
                delta = params.get("delta")
                if isinstance(delta, str):
                    yield delta
            elif message.method == "error":
                params = mapping(message.params, "error")
                error = mapping(params.get("error"), "error")
                if usage_limit_error(error):
                    raise ReviewBackendError(
                        "Codex subscription usage limit reached.", usage_limited=True
                    )
                if params.get("willRetry") is True:
                    continue

                message_text = str(error.get("message") or "unknown failure")
                raise ReviewBackendError(f"Codex failed: {message_text}")
            elif message.method == "turn/completed":
                params = mapping(message.params, "turn completion")
                completed = CodexTurn.model_validate(mapping(params.get("turn"), "turn"))
                if completed.id != self.turn_id:
                    continue

                self.turn_id = None
                if completed.status == "failed":
                    message_text = str((completed.error or {}).get("message") or "unknown failure")
                    raise ReviewBackendError(
                        f"Codex failed: {message_text}",
                        usage_limited=usage_limit_error(completed.error),
                    )


async def stop_process(process: asyncio.subprocess.Process) -> None:
    """Stop an app-server subprocess without allowing cleanup to consume the review deadline."""

    if process.returncode is not None:
        return

    try:
        await asyncio.wait_for(process.communicate(), timeout=5)
    except TimeoutError:
        logger.warning("Codex app-server did not exit after stdin closed; killing it.")
        process.kill()
        try:
            await asyncio.wait_for(process.communicate(), timeout=5)
        except TimeoutError:
            logger.error("Codex app-server did not exit after being killed.")


@asynccontextmanager
async def app_server(*, reviewing: bool = True) -> AsyncIterator[CodexAppServer]:
    """Create an authenticated Codex app-server and clean it up after use."""

    if shutil.which("codex") is None:
        raise ReviewBackendError("Codex CLI is not installed.")

    try:
        json.loads(SETTINGS.codex_auth_json)
    except json.JSONDecodeError as exc:
        raise ReviewBackendError("codex-auth-json is not valid JSON.") from exc

    with tempfile.TemporaryDirectory(prefix="code-review-codex-") as home:
        home_path = Path(home)
        auth_path = home_path / "auth.json"
        auth_path.write_text(SETTINGS.codex_auth_json, encoding="utf-8")
        auth_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        environment = dict(os.environ)
        environment["CODEX_HOME"] = home
        environment.pop("CODEX_AUTH_JSON", None)
        environment.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        process = await asyncio.create_subprocess_exec(
            "codex",
            "app-server",
            "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
            limit=APP_SERVER_LINE_LIMIT,
        )
        client = CodexAppServer(process)
        try:
            await client.initialize(reviewing=reviewing)
            yield client
        finally:
            await stop_process(process)


@asynccontextmanager
async def review_session(pr: PullRequestContext, inputs: ReviewInputs) -> AsyncIterator[ReviewSessionStreams]:
    """Open a Codex review thread whose active turn can be interrupted and flushed."""

    async with app_server() as client:
        yield ReviewSessionStreams(
            review_text=lambda: client.turn_text(pull_request_message(inputs)),
            flush_text=lambda: client.turn_text(flush_prompt(), interrupt=True),
        )


async def generate_text(prompt: str) -> str:
    """Run one Codex turn and return its complete assistant text."""

    output: list[str] = []
    async with app_server(reviewing=False) as client:
        async for chunk in client.turn_text(prompt):
            output.append(chunk)

    return "".join(output)
