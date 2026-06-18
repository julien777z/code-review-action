import json
import logging
import os
import re
import subprocess
from typing import Final

from code_review.config import CONFIG, SETTINGS
from code_review.models.shared.findings import ReviewPayload
from code_review.models.shared.pull_request import PullRequestContext
from code_review.models.shared.threads import ReviewThread

logger = logging.getLogger("code_review.github")

HUNK_HEADER: Final[re.Pattern[str]] = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def run_gh(args: list[str]) -> str:
    """Run a `gh` command with the configured token and return stdout."""

    result = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "GH_TOKEN": SETTINGS.github_token},
    )

    return result.stdout


def fetch_pull_request(repo: str, pr_number: int) -> PullRequestContext:
    """Fetch the PR's current metadata."""

    raw = run_gh(
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


def current_head_sha(repo: str, pr_number: int) -> str:
    """Return the PR's current head SHA."""

    return run_gh(
        ["pr", "view", str(pr_number), "--repo", repo, "--json", "headRefOid", "--jq", ".headRefOid"]
    ).strip()


def pull_request_diff(repo: str, pr_number: int) -> str:
    """Return the PR's unified diff."""

    return run_gh(["pr", "diff", str(pr_number), "--repo", repo])


def already_reviewed(repo: str, pr_number: int, head_sha: str, marker: str) -> bool:
    """Return True if this tier already posted a review (carrying its marker) for the given head."""

    raw = run_gh(
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


def head_check_concluded(repo: str, head_sha: str) -> bool:
    """Return True if a completed review check run already exists for this head commit."""

    raw = run_gh(
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


def start_check_run(repo: str, head_sha: str) -> str | None:
    """Open an in-progress review check run and return its id."""

    try:
        raw = run_gh(
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


def complete_check_run(
    repo: str, check_id: str | None, conclusion: str, title: str, summary: str
) -> bool:
    """Conclude the review check run with the round's verdict; return whether it is no longer pending."""

    if check_id is None:
        return True

    try:
        run_gh(
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


def list_review_threads(repo: str, pr_number: int) -> list[ReviewThread]:
    """List every review thread on the PR via GraphQL, paginating fully; raise on a partial fetch."""

    owner, _, name = repo.partition("/")
    list_query = (
        "query($owner:String!,$name:String!,$number:Int!,$after:String){"
        "repository(owner:$owner,name:$name){pullRequest(number:$number){"
        "reviewThreads(first:100,after:$after){pageInfo{hasNextPage endCursor} "
        "nodes{id isResolved isOutdated comments(first:1){nodes{author{login} body path}}}}}}}"
    )

    threads: list[ReviewThread] = []
    after = None
    try:
        while True:
            args = [
                "api",
                "graphql",
                "-f",
                f"query={list_query}",
                "-f",
                f"owner={owner}",
                "-f",
                f"name={name}",
                "-F",
                f"number={pr_number}",
            ]
            if after is not None:
                args += ["-f", f"after={after}"]

            page = json.loads(run_gh(args))["data"]["repository"]["pullRequest"]["reviewThreads"]
            threads.extend(ReviewThread.model_validate(node) for node in page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                break

            after = page["pageInfo"]["endCursor"]
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError, TypeError) as exc:
        # Fail loudly: approving over open threads dropped by a partial fetch would be a false success.
        logger.error("Could not list review threads to reconcile: %s", exc)

        raise

    return threads


def resolve_threads(repo: str, thread_ids: list[str]) -> None:
    """Resolve the given review threads — run only after the head is confirmed and the review posted."""

    mutation = "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{id}}}"

    for thread_id in thread_ids:
        try:
            run_gh(["api", "graphql", "-f", f"query={mutation}", "-f", f"id={thread_id}"])
        except subprocess.CalledProcessError as exc:
            logger.warning("Could not resolve a review thread: %s", exc)


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


def diff_anchors(repo: str, pr_number: int) -> tuple[dict[str, tuple[set[int], set[int]]], set[str]]:
    """Map patched changed files to their (RIGHT, LEFT) anchor lines, plus files GitHub gave no patch."""

    raw = run_gh(
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


def post_review(repo: str, pr_number: int, payload: ReviewPayload) -> bool:
    """Post the review (inline comments + summary) in one call; return False if GitHub rejects it."""

    process = subprocess.run(
        ["gh", "api", "--method", "POST", f"repos/{repo}/pulls/{pr_number}/reviews", "--input", "-"],
        input=payload.model_dump_json(),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GH_TOKEN": SETTINGS.github_token},
    )

    if process.returncode != 0:
        logger.error("Review POST failed (%s): %s", process.returncode, process.stderr.strip())

        return False

    logger.info("Posted review: %s", process.stdout.strip())

    return True
