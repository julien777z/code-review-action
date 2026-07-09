# Review criteria

The shared scope, severity rubric, and false-positive rules for the code-review skill and its CI variant. Both the interactive `SKILL.md` flow and the CI `CI_REVIEW.md` flow apply these criteria; keep this file the single source of truth for them.

**Scope — review only what this PR changes.** Limit the review to the lines this PR adds or modifies. Every finding must anchor to a line in the diff; do not go hunting for pre-existing problems in untouched code. If a real issue sits on a line the PR did not touch — even in a file the PR edited — leave it out.

## Severity

Assign each finding a severity.

- **Critical** — data loss, security/auth bypass, a crash, or clearly broken core behavior.
- **High** — a real bug likely hit in normal use, or a clear violation of a project rule that matters in practice.
- **Medium** — a real issue with limited, conditional, or non-obvious impact.
- **Low** — valid but minor: a nitpick the change genuinely got wrong, a rare edge case, or a small correctness/UX deviation.

**Calibrate severity by how likely the trigger is, not by how severe the worst case sounds.** An issue that only manifests under unlikely timing, a race, or concurrent runs — or whose impact is narrowly scoped (e.g. only one runner's own data) or self-corrects on the next run — is **at most Medium**, and Low when the effect is trivial or cosmetic. Reserve **High** for a bug whose trigger is common in normal use. Do not rate a rare, scoped, or recoverable edge case High just because its failure mode reads as serious.

**Be selective — a short, high-signal review beats an exhaustive one.** Report a finding only when you are highly confident it is a real defect a maintainer would act on, with a concrete trigger that is realistically hit. A review carrying many findings is itself a signal you are over-flagging: keep the few that clearly matter and drop the rest. In particular, **do not report**:
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
