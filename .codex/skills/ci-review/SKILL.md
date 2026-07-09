---
name: "ci-review"
description: "CI-runner adaptation of the code-review skill. Applies code-review's lenses and severity rubric to a pull request inside a CI job, where the runner handles PR discovery, posting, and thread resolution and findings stream out as JSONL. Use when a CI runner drives the review."
---

# Code Review (CI runner)

Run the `code-review` skill (included after this section) to review the pull request, adapted for a CI runner. Apply code-review's scope, review lenses, severity rubric, calibration, selectivity bar, and false-positive rules **unchanged** — the adaptations below only change what the runner already does for you and how you return findings.

## The runner owns the GitHub work

The CI runner has already verified eligibility, fetched the diff (embedded in your prompt), and listed previously posted findings (embedded in your prompt); it posts every comment, re-gates the head commit, and resolves stale threads itself. So, overriding the corresponding code-review steps:

- **Skip Step 1 (eligibility) and Step 2 (context fetch).** Do not discover the PR, check whether it is closed / draft / already reviewed, or fetch the diff — it is in your prompt.
- **Skip Step 5 (re-gate) and Step 6 (posting and thread resolution).** Do not post a review, resolve threads, or write a summary body — the runner does all of that.
- **Never call GitHub.** The repository checkout is read context only: read source files and run local `git blame` / `git log` on the changed lines, but do not use any GitHub tool.
- In Step 3, **drop the prior-PRs lens** — it needs GitHub history you should not fetch. Keep the rules, bugs, history, and comments lenses.
- In Step 4, **do not fetch existing comments** — the prompt already lists prior findings, so re-report any that still apply with the same path and title. **Do not cap or drop low findings yourself** — emit every finding that clears the severity bar and let the runner apply the low-findings cap.

## Review as a single agent, file by file

Do the review yourself as one agent — do **not** fan out to sub-agents. Take code-review's single-thread path: work through the changed files one at a time and apply the review lenses to each file in that one thread. Sub-agents each re-read the diff and rules, which multiplies the work and delays your first finding while you wait for them to return.

## Emit findings incrementally as JSONL

The runner posts the review from the JSONL you stream (the exact line format follows this skill), not from inline comments. So, replacing code-review's single validate-then-post pass:

- Emit each finding the moment you validate it — as you finish each file, emit its findings on their own lines and move to the next file. **Never** hold findings for a global ranking, sort, or dedup pass, and never wait until the review is complete to emit the first one. The runner deduplicates, orders, and caps, so partial progress is never wasted — if the run is cut off, every finding you already emitted is kept.
