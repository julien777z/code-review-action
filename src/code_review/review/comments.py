from code_review.config import CONFIG, DISCLAIMER
from code_review.models.findings import Finding, ReviewCommentRequest, ReviewPayload
from code_review.models.review import CheckConclusion
from code_review.models.severity import Severity
from code_review.models.threads import ThreadCommentNode


def thread_title(comment: ThreadCommentNode) -> str | None:
    """Return the finding title from this tier's comment body."""

    return next((row[4:].strip() for row in comment.body.splitlines() if row.startswith("### ")), None)


def thread_severity(comment: ThreadCommentNode) -> Severity | None:
    """Return the severity from this tier's current fixed severity line."""

    split_body = comment.body.split(CONFIG["untrusted_input_open"], 1)
    if len(split_body) != 2:
        return None

    fenced_body = split_body[1].split(CONFIG["untrusted_input_close"], 1)[0]
    lines = [row.strip() for row in fenced_body.splitlines()]
    try:
        heading_index = next(index for index, row in enumerate(lines) if row.startswith("### "))
    except StopIteration:
        return None

    line = next((row for row in lines[heading_index + 1 :] if row), "")
    severity_line = line.split("<br>", 1)[0]
    if not severity_line.startswith("**") or not severity_line.endswith(" Severity**"):
        return None

    severity_text = severity_line.removeprefix("**").removesuffix(" Severity**")
    try:
        return Severity.from_str(severity_text)
    except ValueError:
        return None


def finding_severity_line(finding: Finding) -> str:
    """Return the prominent severity line shown below the finding title."""

    return f"**{finding.severity.value.capitalize()} Severity**"


def finding_category_footer(finding: Finding) -> str:
    """Return the small category footer for review comments."""

    return f"<sub>{finding.category.label}</sub>"


def review_disclaimer(reviewers: set[str]) -> str:
    """Render action attribution with the model that produced the review content."""

    model = ", ".join(sorted(reviewers))

    return f"{DISCLAIMER}\n\n<sub>Model: {model}</sub>" if model else DISCLAIMER


def comment_body(finding: Finding, marker: str) -> str:
    """Render one inline comment body with category and severity."""

    return (
        f"{CONFIG['untrusted_input_open']}\n"
        f"### {finding.title}\n\n{finding_severity_line(finding)}<br>{finding_category_footer(finding)}\n\n{finding.body}\n"
        f"{CONFIG['untrusted_input_close']}\n\n"
        f"{review_disclaimer({finding.reviewer})}\n\n{marker}"
    )


def build_inline_comment(head_sha: str, finding: Finding, marker: str) -> ReviewCommentRequest:
    """Build the standalone inline comment request for one anchorable finding."""

    return ReviewCommentRequest(
        commit_id=head_sha,
        path=finding.path,
        line=finding.line,
        side=finding.side,
        body=comment_body(finding, marker),
    )


def compute_verdict(open_count: int, open_blocking: bool) -> tuple[str, CheckConclusion, str]:
    """Return the review event, check conclusion, and check title for open issues."""

    if open_count == 0:
        return "APPROVE", CheckConclusion.SUCCESS, "No unresolved issues"

    if open_blocking:
        return "REQUEST_CHANGES", CheckConclusion.FAILURE, "Blocking issue open"

    plural = "s" if open_count != 1 else ""

    return "COMMENT", CheckConclusion.NEUTRAL, f"{open_count} unresolved issue{plural}"


def verdict_summary(event: str, open_count: int, previous_count: int) -> str:
    """Phrase the verdict as the count of unresolved issues."""

    if event == "APPROVE":
        return "No unresolved issues — approving."

    plural = "s" if open_count != 1 else ""
    verb = "is" if open_count == 1 else "are"
    carried = f" (including {previous_count} from a previous review)" if previous_count else ""
    line = f"There {verb} {open_count} unresolved issue{plural}{carried}."

    if event == "REQUEST_CHANGES":
        return f"{line} A blocking issue is open — requesting changes."

    return line


def build_verdict_review(
    head_sha: str,
    out_of_bounds: list[Finding],
    event: str,
    summary_line: str,
    marker: str,
    reviewers: set[str],
) -> ReviewPayload:
    """Build the final verdict review."""

    body = summary_line
    if out_of_bounds:
        listed = "\n".join(
            f"- {finding.path}:{finding.line} — {finding_severity_line(finding)} — {finding_category_footer(finding)} — "
            f"{finding.body}"
            for finding in out_of_bounds
        )
        body = f"{body}\n\nFindings not posted inline:\n{listed}"

    body = (
        f"{CONFIG['untrusted_input_open']}\n{body}\n{CONFIG['untrusted_input_close']}\n\n"
        f"{review_disclaimer(reviewers)}\n\n{marker}"
    )

    return ReviewPayload(commit_id=head_sha, event=event, body=body, comments=[])
