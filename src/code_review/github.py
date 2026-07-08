import asyncio
import json
import logging
import os
import re
import subprocess
from typing import Final

from code_review.config import CONFIG, SETTINGS
from code_review.models.findings import ReviewCommentRequest, ReviewPayload
from code_review.models.pull_request import PullRequestBodyUpdate, PullRequestContext
from code_review.models.threads import ReviewThread

logger = logging.getLogger("code_review.github")

HUNK_HEADER: Final[re.Pattern[str]] = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
GITHUB_DIFF_TOO_LARGE_PHRASES: Final[tuple[str, ...]] = (
    "diff exceeded",
    "diff is too large",
    "too large to",
    "maximum number of files",
    "http 406",
    "not acceptable",
)


def github_error_text(exc: subprocess.CalledProcessError) -> str:
    """Return combined `gh` error output for message-based classification."""

    parts: list[str] = []
    for value in (exc.stderr, exc.output):
        if isinstance(value, bytes):
            parts.append(value.decode(errors="replace"))
        elif value:
            parts.append(str(value))

    return "\n".join(parts).lower()


def is_diff_too_large(exc: subprocess.CalledProcessError) -> bool:
    """Return True when a `gh pr diff` failure indicates the diff exceeds GitHub's size cap."""

    text = github_error_text(exc)

    return any(phrase in text for phrase in GITHUB_DIFF_TOO_LARGE_PHRASES)


async def run_gh(args: list[str], stdin: str | None = None, token: str | None = None) -> str:
    """Run a `gh` command with the given token (or the default github token) and return stdout."""

    process = await asyncio.create_subprocess_exec(
        "gh",
        *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "GH_TOKEN": token or SETTINGS.github_token},
    )
    stdout, stderr = await process.communicate(stdin.encode() if stdin is not None else None)

    if process.returncode != 0:
        raise subprocess.CalledProcessError(
            process.returncode, ["gh", *args], output=stdout.decode(), stderr=stderr.decode()
        )

    return stdout.decode()


async def fetch_pull_request(repo: str, pr_number: int) -> PullRequestContext:
    """Fetch the PR's current metadata."""

    raw = await run_gh(
        [
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo,
            "--json",
            "url,headRefName,headRefOid,author,state,isDraft",
        ]
    )
    data = json.loads(raw)

    return PullRequestContext(
        repo=repo,
        number=pr_number,
        head_sha=data["headRefOid"],
        head_ref=data["headRefName"],
        url=data["url"],
        author=(data.get("author") or {}).get("login", ""),
        is_draft=bool(data.get("isDraft")),
        state=data["state"],
    )


async def current_head_sha(repo: str, pr_number: int) -> str:
    """Return the PR's current head SHA."""

    raw = await run_gh(
        ["pr", "view", str(pr_number), "--repo", repo, "--json", "headRefOid", "--jq", ".headRefOid"]
    )

    return raw.strip()


async def pull_request_diff(repo: str, pr_number: int) -> str:
    """Return the PR's unified diff."""

    return await run_gh(["pr", "diff", str(pr_number), "--repo", repo])


async def pull_request_diff_if_available(repo: str, pr_number: int) -> str | None:
    """Return the PR diff, or None when GitHub refuses an oversized diff."""

    try:
        return await pull_request_diff(repo, pr_number)
    except subprocess.CalledProcessError as exc:
        if is_diff_too_large(exc):
            return None

        raise


async def pull_request_body(repo: str, pr_number: int) -> str:
    """Return the PR's current description text."""

    raw = await run_gh(["pr", "view", str(pr_number), "--repo", repo, "--json", "body"])

    return json.loads(raw).get("body") or ""


async def update_pull_request_body(repo: str, pr_number: int, body: str) -> None:
    """Replace the PR's description."""

    await run_gh(
        ["api", "--method", "PATCH", f"repos/{repo}/pulls/{pr_number}", "--input", "-"],
        stdin=PullRequestBodyUpdate(body=body).model_dump_json(),
    )


async def already_reviewed(repo: str, pr_number: int, head_sha: str, marker: str) -> bool:
    """Return True if a review carrying the marker already exists for the given head."""

    raw = await run_gh(
        [
            "api",
            "--paginate",
            f"repos/{repo}/pulls/{pr_number}/reviews",
            "--jq",
            '.[] | select(.state != "PENDING" and .state != "DISMISSED" '
            f'and ((.body // "") | contains("{marker}"))) | .commit_id',
        ]
    )

    return head_sha in raw.split()


async def head_check_concluded(repo: str, head_sha: str) -> bool:
    """Return True if a completed review check run already exists for this head commit."""

    raw = await run_gh(
        [
            "api",
            "--paginate",
            f"repos/{repo}/commits/{head_sha}/check-runs",
            "--jq",
            f'.check_runs[] | select(.name == "{CONFIG["status_check_name"]}" '
            'and .status == "completed" and (.conclusion == "success" '
            'or .conclusion == "neutral" or .conclusion == "failure")) | .id',
        ]
    )

    return bool(raw.split())


async def start_check_run(repo: str, head_sha: str) -> str | None:
    """Open an in-progress review check run and return its id."""

    try:
        raw = await run_gh(
            [
                "api",
                "--method",
                "POST",
                f"repos/{repo}/check-runs",
                "-f",
                f"name={CONFIG['status_check_name']}",
                "-f",
                f"head_sha={head_sha}",
                "-f",
                "status=in_progress",
                "-f",
                "output[title]=Code review",
                "-f",
                "output[summary]=Reviewing the changes…",
            ]
        )

        return str(json.loads(raw)["id"])
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError) as exc:
        logger.warning("Could not open the review check run: %s", exc)

        return None


async def complete_check_run(
    repo: str, check_id: str | None, conclusion: str, title: str, summary: str
) -> bool:
    """Conclude the review check run with the round's verdict; return whether it is no longer pending."""

    if check_id is None:
        return True

    try:
        await run_gh(
            [
                "api",
                "--method",
                "PATCH",
                f"repos/{repo}/check-runs/{check_id}",
                "-f",
                "status=completed",
                "-f",
                f"conclusion={conclusion}",
                "-f",
                f"output[title]={title}",
                "-f",
                f"output[summary]={summary}",
            ]
        )

        return True
    except subprocess.CalledProcessError as exc:
        logger.warning("Could not conclude the review check run: %s", exc)

        return False


async def list_review_threads(repo: str, pr_number: int) -> list[ReviewThread]:
    """List every review thread on the PR via GraphQL, paginating fully; raise on a partial fetch."""

    owner, _, name = repo.partition("/")
    list_query = (
        "query($owner:String!,$name:String!,$number:Int!,$endCursor:String){"
        "repository(owner:$owner,name:$name){pullRequest(number:$number){"
        "reviewThreads(first:100,after:$endCursor){pageInfo{hasNextPage endCursor} "
        "nodes{id isResolved isOutdated comments(first:1){nodes{author{login} body path}}}}}}}"
    )

    try:
        raw = await run_gh(
            [
                "api",
                "graphql",
                "--paginate",
                "-f",
                f"query={list_query}",
                "-f",
                f"owner={owner}",
                "-f",
                f"name={name}",
                "-F",
                f"number={pr_number}",
                "--jq",
                ".data.repository.pullRequest.reviewThreads.nodes[]",
            ]
        )
        threads = [ReviewThread.model_validate(json.loads(line)) for line in raw.splitlines() if line.strip()]
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError, TypeError) as exc:
        # Fail loudly: approving over open threads dropped by a partial fetch would be a false success.
        logger.error("Could not list review threads to reconcile: %s", exc)

        raise

    return threads


async def resolve_threads(repo: str, thread_ids: list[str]) -> None:
    """Resolve the given review threads with the resolve token; the default github token cannot resolve."""

    # The default Actions GITHUB_TOKEN cannot call resolveReviewThread (GitHub rejects it with
    # "Resource not accessible by integration"), so use the elevated resolve token when one is set.
    token = SETTINGS.resolve_token or SETTINGS.github_token
    mutation = "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{id}}}"

    for thread_id in thread_ids:
        try:
            await run_gh(["api", "graphql", "-f", f"query={mutation}", "-f", f"id={thread_id}"], token=token)
        except subprocess.CalledProcessError as exc:
            logger.warning("Could not resolve review thread %s: %s", thread_id, (exc.stderr or "").strip())


def parse_patch(patch: str) -> tuple[set[int], set[int]]:
    """Return the (RIGHT new-side, LEFT old-side) line numbers a unified-diff patch exposes."""

    right: set[int] = set()
    left: set[int] = set()
    old_line = 0
    new_line = 0
    in_hunk = False

    for raw in patch.splitlines():
        header = HUNK_HEADER.match(raw)
        if header is not None:
            old_line = int(header.group(1))
            new_line = int(header.group(2))
            in_hunk = True

            continue

        if not in_hunk:
            continue

        if raw.startswith("+"):
            right.add(new_line)
            new_line += 1
        elif raw.startswith("-"):
            left.add(old_line)
            old_line += 1
        elif raw.startswith(" "):
            right.add(new_line)
            left.add(old_line)
            new_line += 1
            old_line += 1

    return right, left


async def diff_anchors(repo: str, pr_number: int) -> tuple[dict[str, tuple[set[int], set[int]]], set[str]]:
    """Map patched changed files to their (RIGHT, LEFT) anchor lines, plus files GitHub gave no patch."""

    raw = await run_gh(
        [
            "api",
            "--paginate",
            f"repos/{repo}/pulls/{pr_number}/files",
            "--jq",
            ".[] | {filename, patch}",
        ]
    )

    anchors: dict[str, tuple[set[int], set[int]]] = {}
    unpatched: set[str] = set()

    for line in raw.splitlines():
        if not line.strip():
            continue

        entry = json.loads(line)
        patch = entry.get("patch")
        if patch:
            anchors[entry["filename"]] = parse_patch(patch)
        else:
            unpatched.add(entry["filename"])

    return anchors, unpatched


async def post_review(repo: str, pr_number: int, payload: ReviewPayload) -> bool:
    """Post the review (inline comments + summary) in one call; return False if GitHub rejects it."""

    try:
        stdout = await run_gh(
            ["api", "--method", "POST", f"repos/{repo}/pulls/{pr_number}/reviews", "--input", "-"],
            stdin=payload.model_dump_json(),
        )
    except subprocess.CalledProcessError as exc:
        logger.error("Review POST failed (%s): %s", exc.returncode, (exc.stderr or "").strip())

        return False

    logger.info("Posted review: %s", stdout.strip())

    return True


async def post_comment(repo: str, pr_number: int, payload: ReviewCommentRequest) -> bool:
    """Post one standalone inline review comment; return False if GitHub rejects it."""

    try:
        stdout = await run_gh(
            ["api", "--method", "POST", f"repos/{repo}/pulls/{pr_number}/comments", "--input", "-"],
            stdin=payload.model_dump_json(),
        )
    except subprocess.CalledProcessError as exc:
        logger.error("Comment POST failed (%s): %s", exc.returncode, (exc.stderr or "").strip())

        return False

    logger.info("Posted inline comment: %s", stdout.strip())

    return True


async def add_reaction(subject_path: str) -> int | None:
    """React with eyes on an issue or comment to show a review is in progress; return the reaction id."""

    try:
        raw = await run_gh(["api", "--method", "POST", f"{subject_path}/reactions", "-f", "content=eyes"])

        return int(json.loads(raw)["id"])
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.warning("Could not add the reviewing reaction: %s", exc)

        return None


async def remove_reaction(subject_path: str, reaction_id: int) -> None:
    """Remove a previously-added reviewing reaction; best-effort."""

    try:
        await run_gh(["api", "--method", "DELETE", f"{subject_path}/reactions/{reaction_id}"])
    except subprocess.CalledProcessError as exc:
        logger.warning("Could not remove the reviewing reaction: %s", exc)
