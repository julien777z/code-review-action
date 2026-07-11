---
name: code-review-loop
description: Run code-review repeatedly on the complete current-branch PR diff, investigate and fix every legitimate finding, validate and push the fixes, and continue until a fresh review has no remaining findings. Use when asked to review-and-fix, keep reviewing until clean, or run a code review loop.
---

# Code Review Loop

Drive the branch from its current state to a clean review result. Use `code-review` for every review pass so branch/PR creation, full-diff scope, review lenses, severity, and chat-only reporting stay consistent.

## Loop

1. Run `code-review` against the complete PR diff. Allow it to create the feature branch, commit/push intended changes, and create a draft PR when the current branch has no matching PR.
2. Validate every reported finding against the code, task intent, repository rules, and a concrete trigger.
   - Fix every legitimate issue.
   - Record disproven findings in a session-local dismissal ledger keyed by path and short title, with the evidence, so later passes do not repeat them.
   - Never dismiss a finding merely because the fix is inconvenient or expands the changed-file set.
3. Apply the smallest complete fixes while preserving the requested behavior and unrelated worktree changes.
4. Run focused tests, typechecks, builds, or other checks appropriate to the fixes. Do not hide failures; distinguish new failures from verified pre-existing ones.
5. Stage only the intended task changes and review fixes, commit them, and push the current PR branch.
6. Run `code-review` again on the entire updated base-to-head PR diff, supplying the dismissal ledger to the reviewers.
7. Repeat steps 2–6 until a fresh pass reports no new or unresolved legitimate findings.

There is no arbitrary iteration limit. If the same legitimate issue repeats, investigate why the prior fix was incomplete and correct it. Stop only when genuinely blocked by missing product intent, credentials, permissions, or an external dependency; report the exact blocker in chat.

## Head changes

- Treat every pushed fix as a new review baseline.
- If another actor changes the branch during a pass, discard stale results and restart on the new head.
- Review the full PR diff every time, not only the most recent fix commit.

## GitHub boundary

GitHub mutations are limited to creating the branch/draft PR and committing/pushing the implementation and fixes. Never post review bodies, inline comments, issue comments, or thread replies. All findings, dismissals, iteration updates, and the final clean result belong only in the current chat session.

## Completion report

When clean, report in chat:

- the number of review passes;
- the legitimate issues fixed and any findings dismissed with evidence;
- validation commands and results;
- the PR URL and final head SHA;
- confirmation that the final full-diff review returned no findings.
