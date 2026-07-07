# Code Review Action

A GitHub Action that reviews pull requests with **Claude** or **Cursor** and posts severity-rated
**inline comments** plus an **Approval Verdict** check run. It reviews only the lines a PR changes,
caps nitpicks, and reconciles its own threads across pushes.

## Quick start

Add a workflow to your repo:

```yaml
name: Code Review
on:
  pull_request:
    types: [opened, synchronize, ready_for_review]
  issue_comment:
    types: [created]

permissions:
  contents: read
  pull-requests: write
  checks: write

jobs:
  review:
    name: Review
    runs-on: ubuntu-latest
    concurrency:
      group: code-review-${{ github.event.pull_request.number || github.event.issue.number }}-${{ github.event_name == 'issue_comment' && 'comment' || 'review' }}
      cancel-in-progress: true
    steps:
      - uses: actions/checkout@v4 # lets the Cursor backend load your .cursor/rules
      - uses: julien777z/code-review-action@v0
        with:
          cursor-api-key: ${{ secrets.CURSOR_API_KEY }}
```

Provide at least one backend credential (`anthropic-api-key` or `cursor-api-key`). Comment
`agent review` on a PR to trigger a manual review.

## Examples

Each snippet is the `with:` block for the step in the Quick start workflow — swap it in.

Use Claude instead of Cursor:

```yaml
with:
  anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
```

Claude on the first review, Cursor on later pushes:

```yaml
with:
  anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
  cursor-api-key: ${{ secrets.CURSOR_API_KEY }}
  first-review-model: claude
  review-model: cursor
```

Comment only, scoped to source files:

```yaml
with:
  cursor-api-key: ${{ secrets.CURSOR_API_KEY }}
  approval-disable: "true"
  include-paths: "src/**"
  exclude-paths: "**/*.lock"
```

Request changes only on critical or high findings:

```yaml
with:
  cursor-api-key: ${{ secrets.CURSOR_API_KEY }}
  approval-include: "critical, high"
```

## Choosing the model

- `review-model` — `auto` (default), `claude`, or `cursor`. `auto` prefers Claude when an Anthropic
  key is set, otherwise uses Cursor.
- `first-review-model` — optional backend used for the PR's first review (opened / ready for review).
  When empty, `review-model` is used for every event. Example: `first-review-model: claude` with
  `review-model: cursor` reviews the opened PR with Claude and later pushes with Cursor.

## Enforcing project rules

With `enforce-project-rules` on (the default), the review applies your repository's own coding rules
and reports a finding on any changed line that violates them. Each backend loads whatever rule files it
understands (for example `.cursor/rules` or `CLAUDE.md` / `.claude/rules`).

**Cursor backend.** Cursor runs a **local** agent that loads your `.cursor/rules` from the checked-out
repository's working directory. Check out the repo before this action so those files are present:

```yaml
steps:
  - uses: actions/checkout@v4
  - uses: julien777z/code-review-action@v0
    with:
      cursor-api-key: ${{ secrets.CURSOR_API_KEY }}
```

**Claude backend.** Claude reviews through a [Managed
Agents](https://platform.claude.com/docs/en/managed-agents) session that mounts the repository at the
PR's head commit, so Claude Code loads `CLAUDE.md` / `.claude/rules` natively. The repository is cloned
with the run's `github-token` (which needs `contents: read`), so no extra GitHub connection is required.
When `enforce-project-rules` is `false`, the session runs without the repository mounted and reviews
from the diff alone.

- `enforce-project-rules` — set to `false` to skip loading and enforcing the repo's rules (default
  `true`).
- `project-rules-severity` — pin every rule violation to a fixed severity (`critical`, `high`,
  `medium`, or `low`). Empty lets the review rate each violation itself (default empty). Set this above
  `low` when rule violations are being crowded out by `low-findings-cap`.

## Suggesting simplifications

Off by default, the review can also suggest code simplifications (optional suggestions that never
block).

- `simplify-suggest` — apply the agent's `code-simplify` skill to the changed code and suggest
  simplifications (default `false`).
- `simplify-suggest-severity` — severity to report those suggestions at (`critical`, `high`, `medium`,
  or `low`). Empty defaults to `low`; raise it so suggestions are not crowded out by `low-findings-cap`.
- `simplify-nearby-code` — extend those suggestions to weigh the nearby and related code the change
  touches, not just the changed lines in isolation (default `false`). Findings still anchor on changed
  lines.

## Approval behavior

- `approval-include` — severities that request changes when left open (default `critical`). Other
  open findings post as a comment; zero open findings approves.
- `approval-disable` — post comments only and skip the verdict and check run.

## Restricting who can trigger reviews

Use this to control who can spend review runs — for example, to stop outside or first-time
contributors from kicking off a review on every PR while still letting your team request one.

`author-associations` is an allowlist of GitHub author associations allowed to trigger a review —
both on pull-request events and via the `agent review` comment. Leave it empty (the default) to
allow everyone. Valid values: `OWNER`, `MEMBER`, `COLLABORATOR`, `CONTRIBUTOR`,
`FIRST_TIME_CONTRIBUTOR`, `FIRST_TIMER`, `MANNEQUIN`, `NONE`.

Allow anyone (the default):

```yaml
with:
  cursor-api-key: ${{ secrets.CURSOR_API_KEY }}
  author-associations: ""
```

Allow only the repository owner and organization members:

```yaml
with:
  cursor-api-key: ${{ secrets.CURSOR_API_KEY }}
  author-associations: "OWNER, MEMBER"
```

## Inputs

| Input | Default | Description |
|---|---|---|
| `github-token` | `${{ github.token }}` | Token to read the diff and post reviews/checks |
| `resolve-token` | — | Token with pull-request write to resolve the action's own threads (e.g. a GitHub App token) |
| `anthropic-api-key` | — | Anthropic key for the Claude API backend |
| `cursor-api-key` | — | Cursor key for the Cursor backend |
| `review-model` | `auto` | `auto` \| `claude` \| `cursor` |
| `first-review-model` | — | Backend for the first review; empty uses `review-model` |
| `claude-model` | `claude-opus-4-8` | Anthropic model id |
| `cursor-model` | `composer-2.5` | Cursor model id |
| `additional-context` | — | Extra context for the review |
| `approval-include` | `critical` | Severities that request changes when open |
| `approval-disable` | `false` | Comments only; skip the verdict |
| `pr-review-summary` | `true` | Append an AI summary to the PR description on the first review |
| `enforce-project-rules` | `true` | Enforce the repository's own coding rules; no-op when it defines none |
| `project-rules-severity` | — | Fixed severity for rule violations; empty lets the review rate each |
| `simplify-suggest` | `false` | Suggest code simplifications via the code-simplify skill |
| `simplify-suggest-severity` | — | Severity for simplification suggestions; empty defaults to low |
| `simplify-nearby-code` | `false` | Extend simplification suggestions to weigh nearby/related code |
| `min-severity` | `low` | Lowest severity worth posting |
| `low-findings-cap` | `3` | Max low-severity findings per review |
| `max-findings` | — | Overall inline-comment cap (empty = uncapped) |
| `include-paths` | — | Globs to restrict the review to |
| `exclude-paths` | — | Globs to skip |
| `trigger-phrase` | `agent review` | Comment phrase for a manual review |
| `review-drafts` | `true` | Review draft PRs |
| `author-associations` | — | Allowlist of who may trigger; empty allows all |
| `pr-number` | — | PR number for `workflow_dispatch` runs |

## Versioning

Pin the moving major tag for automatic patches:

```yaml
- uses: julien777z/code-review-action@v0
```

## License

MIT
