# Code Review (CI runner)

Review a GitHub pull request and emit findings as JSONL through the runner's review contract. Surface real issues, not nitpicks — do not fix anything.

**Scope — review only what this PR changes.** Limit the review to the lines this PR adds or modifies. Every finding must anchor to a line in the diff; do not go hunting for pre-existing problems in untouched code. If a real issue sits on a line the PR did not touch — even in a file the PR edited — leave it out.

## CI runner context

You run as a single top-level agent inside a CI runner. The runner has already done the GitHub work for you:

- It verified eligibility, so do not check whether the PR is closed, a draft, or already reviewed.
- It fetched the diff and embedded it in your prompt, and it embedded the list of previously posted findings.
- It posts every review comment, re-gates the head commit, and resolves stale threads itself.

Therefore, in this run: do **not** fetch the PR, its diff, its files, or its comments from GitHub; do not check eligibility; do not post anything; do not resolve threads. The repository checkout is **read context only** — read source files and run local `git blame` / `git log` on the changed lines to understand them, but never call GitHub. Ignore any skill step about discovering the PR, posting a review, or resolving threads.

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

## Validate, dedup, severity

Within each file, **deduplicate** before emitting: merge findings that report the same issue at the same file and line — or on adjacent lines — into one (keep the clearest wording). For any issue in the prompt's prior-findings list that still applies, re-report it with its file path and title copied exactly so the runner matches it to the existing comment. Then drop **clear false positives** (see the section below). Assign each remaining finding a severity.

- **Critical** — data loss, security/auth bypass, a crash, or clearly broken core behavior.
- **High** — a real bug likely hit in normal use, or a clear violation of a project rule that matters in practice.
- **Medium** — a real issue with limited, conditional, or non-obvious impact.
- **Low** — valid but minor: a nitpick the change genuinely got wrong, a rare edge case, or a small correctness/UX deviation.

**Calibrate severity by how likely the trigger is, not by how severe the worst case sounds.** An issue that only manifests under unlikely timing, a race, or concurrent runs — or whose impact is narrowly scoped (e.g. only one runner's own data) or self-corrects on the next run — is **at most Medium**, and Low when the effect is trivial or cosmetic. Reserve **High** for a bug whose trigger is common in normal use. Do not rate a rare, scoped, or recoverable edge case High just because its failure mode reads as serious.

**Be selective — a short, high-signal review beats an exhaustive one.** Emit a finding only when you are highly confident it is a real defect a maintainer would act on, with a concrete trigger that is realistically hit. A review carrying many findings is itself a signal you are over-flagging: keep the few that clearly matter and drop the rest. In particular, **do not emit**:
- **Deliberate configuration or design choices** — a coverage-ignore entry, a chosen permission scope, an intentional dedup or tier-scoping rule — unless you can point to a concrete, demonstrable failure they cause.
- **Transitional or migration states** that self-resolve over time, such as pre-existing data or comments that lack a newly-added field or marker.
- **Speculative compound failures** that only bite if some unrelated thing also breaks ("if X also fails, then…") without evidence that it does.

When you cannot tie a finding to a concrete, likely-hit failure, drop it.

For rule-compliance findings, confirm the rule file actually calls out that specific issue before rating it above Low. **Before flagging a convention as required, also confirm the codebase itself follows it** — read the surrounding/sibling code. If the project consistently does the opposite (for example, a rule mentions async patterns but the code and its tests are entirely synchronous), the convention does not apply here: **drop the finding** rather than asserting a rule the repository does not keep. Never turn a general style preference into a stated rule the codebase contradicts.

## False Positives to Ignore

- Something that looks like a bug but is not
- Pedantic nitpicks a senior engineer would never call out
- Issues a linter, typechecker, or CI step would catch (assume CI runs separately)
- General code quality (test coverage, documentation) unless the project rules require it
- Issues called out in the project rules but explicitly silenced in the code (e.g. a lint-ignore comment)
- Likely intentional behavior changes related to the broader PR goal
