# Code Review (CI runner)

Review a GitHub pull request and emit findings as JSONL through the runner's review contract. Surface real issues, not nitpicks — do not fix anything. Apply the scope, severity rubric, and false-positive rules in the **Review criteria** that follow this section.

## CI runner context

You run as a single top-level agent inside a CI runner. The runner has already done the GitHub work for you:

- It verified eligibility, so do not check whether the PR is closed, a draft, or already reviewed.
- It fetched the diff and embedded it in your prompt, and it embedded the list of previously posted findings.
- It posts every review comment, re-gates the head commit, and resolves stale threads itself.

Therefore, in this run: do **not** fetch the PR, its diff, its files, or its comments from GitHub; do not check eligibility; do not post anything; do not resolve threads. The repository checkout is **read context only** — read source files and run local `git blame` / `git log` on the changed lines to understand them, but never call GitHub. Ignore any step about discovering the PR, posting a review, or resolving threads.

## Emit findings incrementally

Emit each finding as soon as you have validated it — the moment a lens or a file is done, emit its findings on their own lines and move on. **Never** hold findings back for a final global ranking, sort, or dedup pass, and never wait until the whole review is complete before emitting the first finding. The runner deduplicates, orders, and caps what you emit, so partial progress is never wasted — if your run is cut off, every finding you already emitted is kept.

## Parallel sub-agents

If your runtime can launch sub-agents (a Cursor Agent tool, a Claude Task tool, or equivalent), fan the review lenses below out as parallel sub-agents to cover a large PR quickly. Sub-agents **return** their findings to you; only you, the top-level agent, emit JSONL. Emit each sub-agent's batch as soon as it returns rather than waiting for all of them.

If sub-agents are unavailable, work through the diff file by file: apply every lens to a file, emit that file's findings, then move to the next file. Either way, emission stays incremental.

## Review lenses

Apply these lenses to the changed lines. Each flags findings with a **file path, line number, and diff side** (`RIGHT` for an added/current line, or `LEFT` for a removed line using the base-side line number):

- **Rules**: audit the changes for compliance with the project rule files loaded for this repository. The rules are guidance for code generation, so not all apply during review.
- **Bugs**: scan the changed lines for real defects; ignore likely false positives.
- **History**: where a change looks suspicious in context, read `git blame` / `git log` of the changed lines and flag bugs that only make sense in light of that history — do not walk the history of every line exhaustively.
- **Comments**: read code comments in the modified files; flag anything in the diff that contradicts them.

Within each file, **deduplicate** before emitting: merge findings that report the same issue at the same file and line — or on adjacent lines — into one (keep the clearest wording). For any issue in the prompt's prior-findings list that still applies, re-report it with its file path and title copied exactly so the runner matches it to the existing comment. Then apply the **Review criteria** below to assign severity and drop false positives.
