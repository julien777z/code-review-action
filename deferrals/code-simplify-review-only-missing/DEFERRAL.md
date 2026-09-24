# code-simplify skill: missing `REVIEW_ONLY.md`

## What is deferred

`src/code_review/prompt.py` declares:

```python
CODE_SIMPLIFY_REVIEW_SKILL_RELATIVE: Final[str] = ".agents/skills/code-simplify/REVIEW_ONLY.md"
```

and loads it at `prompt.py:165` via `load_skill(CODE_SIMPLIFY_REVIEW_SKILL_RELATIVE)`. That file
does not exist in the repository — only `.agents/skills/code-simplify/SKILL.md` is present. As a
result `load_skill()` raises `FileNotFoundError` whenever the code-simplify review-only section is
requested, and two tests fail on a clean `main` tree:

- `tests/review_backends/test_cursor.py::TestReviewSession::test_session_loads_rules_per_enforcement[nearby-code]`
- `tests/review_backends/test_claude.py::TestReviewSession::test_repo_mount_follows_enforcement[nearby-code]`

## Why it is still open

`REVIEW_ONLY.md` existed until it was removed by the Agent Sync commit `b9ee575` ("chore: sync
agent files", PR #45), while the code reference to it was left in place. Choosing the correct fix
requires the maintainer's intent about the code-simplify skill structure:

- **Restore** `.agents/skills/code-simplify/REVIEW_ONLY.md` (the review-only variant of the skill)
  so the existing code reference resolves again, or
- **Update** `prompt.py` to load the consolidated `SKILL.md` (or drop the review-only section)
  if the review-only content was intentionally folded into `SKILL.md`.

Either path is a behavior decision about what the code-simplify backend should feed the reviewer,
so it is not safe to guess as part of an unrelated dependency-update pass.

## Scope of picking it up

1. Decide the intended code-simplify skill layout (separate `REVIEW_ONLY.md` vs. consolidated
   `SKILL.md`).
2. Either restore `REVIEW_ONLY.md` with the intended review-only content, or change
   `CODE_SIMPLIFY_REVIEW_SKILL_RELATIVE` / the `prompt.py` load path accordingly.
3. Confirm both tests above pass, plus the rest of `tests/review_backends`.

## Supporting material

| File | What it is |
| --- | --- |
| `DEFERRAL.md` | This record. |

The reference commit is `b9ee575` (PR #45); the code reference is `src/code_review/prompt.py:13`
and `:165`.
