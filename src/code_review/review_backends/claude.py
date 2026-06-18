import logging
import urllib.error
import urllib.request
from typing import Final

import anthropic

from code_review import review
from code_review.config import CONFIG, SETTINGS
from code_review.github import already_reviewed, current_head_sha
from code_review.models.claude.findings import ReviewFindings
from code_review.models.claude.routine import RoutineFireRequest
from code_review.models.shared.findings import Finding
from code_review.models.shared.pull_request import PullRequestContext, ReviewInputs
from code_review.prompt import pull_request_message, review_instructions

logger = logging.getLogger("code_review.claude")

CLAUDE_MAX_TOKENS: Final[int] = 16000


def run_claude_api_review(pr: PullRequestContext) -> int:
    """Review the PR with the Claude Messages API (structured output) and post the result."""

    def _findings(inputs: ReviewInputs) -> list[Finding]:
        client = anthropic.Anthropic(api_key=SETTINGS.anthropic_api_key)
        try:
            response = client.messages.parse(
                model=SETTINGS.claude_model,
                max_tokens=CLAUDE_MAX_TOKENS,
                thinking={"type": "adaptive"},
                system=[
                    {
                        "type": "text",
                        "text": review_instructions(),
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": pull_request_message(inputs)}],
                output_format=ReviewFindings,
            )
        except anthropic.APIError as exc:
            raise review.ReviewBackendError(f"Claude review request failed: {exc}") from exc

        parsed = response.parsed_output
        if parsed is None:
            raise review.ReviewBackendError("Claude returned no structured findings.")

        return list(parsed.findings)

    return review.run_review_round(pr, CONFIG["claude_marker"], _findings)


def build_routine_text(pr: PullRequestContext) -> str:
    """Compose the routine fire prompt: PR context plus the review policy and extra context."""

    lines = [
        f"Review pull request #{pr.number} ({pr.url}) in repo {pr.repo}, on branch {pr.head_ref}, "
        f"opened by {pr.author}, triggered by commit {pr.head_sha}.",
        f"Follow your code-review skill and report findings at or above {SETTINGS.min_severity.value} severity.",
    ]

    if SETTINGS.approval_disable:
        lines.append("Post review comments only; do not post an approval verdict.")
    else:
        included = ", ".join(sorted(severity.value for severity in SETTINGS.approval_include))
        lines.append(f"Request changes when an open finding is one of: {included}.")

    if SETTINGS.additional_context:
        lines.append(f"Additional reviewer context: {SETTINGS.additional_context}")

    return " ".join(lines)


def fire_claude_routine(pr: PullRequestContext) -> int:
    """Fire the hosted Claude review routine for the current PR (the routine posts the review itself)."""

    if not SETTINGS.claude_routine_id or not SETTINGS.claude_routine_api_key:
        logger.error("Claude routine mode needs a routine id and api key.")

        return 1

    if already_reviewed(pr.repo, pr.number, pr.head_sha, CONFIG["claude_marker"]):
        logger.info("Head %s already reviewed by Claude; not firing the routine.", pr.head_sha)

        return 0

    if current_head_sha(pr.repo, pr.number) != pr.head_sha:
        logger.info("Head moved since the event; not firing for superseded commit %s.", pr.head_sha)

        return 0

    request_body = RoutineFireRequest(text=build_routine_text(pr))
    request = urllib.request.Request(
        f"{CONFIG['routine_host']}/{SETTINGS.claude_routine_id}/fire",
        data=request_body.model_dump_json().encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {SETTINGS.claude_routine_api_key}",
            "anthropic-version": CONFIG["anthropic_version"],
            "anthropic-beta": CONFIG["routine_beta"],
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request) as response:
            logger.info("Fired Claude review routine (%s).", response.status)

        return 0
    except urllib.error.HTTPError as exc:
        logger.error("Routine fire failed (%s): %s", exc.code, exc.read().decode(errors="replace"))

        return 1
