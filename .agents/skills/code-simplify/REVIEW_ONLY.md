# Code Simplify Review

Use this read-only CI review variant when simplification suggestions should be emitted as review findings.

Do not edit files, apply fixes, launch sub-agents, post comments, or describe local changes as made. Use this rubric only to decide which JSONL findings to emit through the runner's review contract.

Be ambitious about structural simplification. Look for changes that preserve behavior while making the implementation simpler, smaller, more direct, and easier to maintain.

Flag changed code when it introduces or preserves:

- avoidable indirection, wrappers, or pass-through helpers
- duplicated logic instead of a canonical helper
- branching complexity that should be modeled more directly
- feature logic in the wrong layer or module
- unclear type or boundary contracts
- file growth that should be decomposed
- sequential orchestration or partial updates that make the flow harder to reason about

Prefer high-conviction comments over cosmetic nits. Anchor every suggestion on a changed line and report it as a `code_simplification` category finding.
