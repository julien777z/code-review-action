---
name: propagate-skill
description: Propagate explicitly requested skills, rules, and their changes across the user's other local repositories, creating missing applicable copies while preserving repository-specific guidance. Use when the user invokes /propagate-skill or $propagate-skill alongside a skill or rule creation or update request.
---

# Propagate Skill Changes

Replicate the requested semantic change, not the originating skill file wholesale.

## Workflow

1. Identify every skill or rule created, updated, or explicitly invoked in the same user message, excluding `propagate-skill` itself only when it is the workflow trigger rather than an artifact to propagate. Resolve skill sources at `.agents/skills/<name>/SKILL.md` and rule sources at `.agents/rules/<name>.md`.
2. Determine whether the workspace is on the user's local computer or in a cloud or ephemeral environment. Use filesystem layout, repository remotes, environment markers, and accessible sibling repositories as evidence. If the environment is cloud or remains ambiguous, do not search broadly or mutate other repositories; report the limitation or ask the user.
3. On a local computer, find the repository collection containing the current repository by inspecting its parent directories and nearby Git worktrees. Search bounded repository roots first; do not crawl the entire home directory when the collection root is discoverable.
4. Establish the in-scope target repositories from the bounded collection and the user's request. For every explicitly named skill, update its existing `.agents/skills/<name>/SKILL.md` or create the missing skill directory and source file in each applicable target. For every explicitly named rule, update its existing `.agents/rules/<name>.md` or create it only where the rule applies to that repository's languages, tools, and workflow. Apply propagation only inside the named `.agents/skills/<name>/` or `.agents/rules/<name>.md` paths; never create or edit provider mirrors such as `.codex`, `.claude`, or `.cursor`.
5. Before comparing, read the complete originating file and the complete existing target file (when present), plus enough nearby target guidance to understand its repository-specific context. Never infer that files are identical from matching names, prior propagation, hashes, or a partial diff. Then isolate the semantic delta requested in the current message. Scan the delta's wording, paths, commands, product names, domain terms, and code examples for assumptions specific to the originating repository. Rewrite those parts generically for each target while preserving the requested behavior, then apply the delta in the target's own structure and wording.
6. If origin-specific guidance is irrelevant to or conflicts with a target and cannot be generalized without changing its meaning, omit it from that target or remove it if replication already introduced it. Keep it unchanged in the origin, record the exact conflict, and ask whether the originating skill should also be corrected. Never overwrite conflicting target-specific guidance.
7. Treat guidance present only in another repository as a separate consistency opportunity, not part of the requested replication. Preserve it in place, summarize the specific difference, and ask whether it should also be merged into the originating skill and the other matching repositories.
8. Run relevant validation directly against each changed `.agents` skill or rule when available. Never run agent-sync or source-to-mirror automation; repository CI owns mirror generation. Do not commit or push unless the user also requested it.
9. Report the repositories updated, skipped, or blocked; the requested delta applied; validation results; and any repository-specific guidance awaiting the user's consistency decision.

## Guardrails

- Preserve dirty worktrees and unrelated changes.
- Create a missing same-named skill when the user explicitly names it for propagation; create a missing named rule only in repositories where it applies. Do not infer authority to create unrequested artifacts.
- Author and replicate only `.agents` skills and rules, even when model-specific folders already exist.
- Keep replicated code examples and reusable instructions repository-neutral; retain domain-specific material only where it is relevant to that target.
- Do not turn textual similarity into authority: merge only the current request's intended behavior.
- Never propagate secrets, absolute machine paths, generated mirrors, or repository-specific operational details to unrelated repositories.
