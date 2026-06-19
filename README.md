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
    runs-on: ubuntu-latest
    concurrency:
      group: code-review-${{ github.event.pull_request.number || github.event.issue.number }}-${{ github.event_name == 'issue_comment' && 'comment' || 'review' }}
      cancel-in-progress: true
    steps:
      - uses: julien777z/code-review-action@v0
        with:
          cursor-api-key: ${{ secrets.CURSOR_API_KEY }}
```

Provide at least one backend credential (`anthropic-api-key`, `cursor-api-key`, or the
`claude-routine-*` pair). Comment `agent review` on a PR to trigger a manual review.

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
  anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
  approval-disable: "true"
  include-paths: "src/**"
  exclude-paths: "**/*.lock"
```

Request changes only on critical or high findings:

```yaml
with:
  anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
  approval-include: "critical, high"
```

## Choosing the model

- `review-model` — `auto` (default), `claude`, or `cursor`. `auto` prefers Claude when an Anthropic
  key (or routine credentials) is set, otherwise uses Cursor.
- `first-review-model` — optional backend used for the PR's first review (opened / ready for review).
  When empty, `review-model` is used for every event. Example: `first-review-model: claude` with
  `review-model: cursor` reviews the opened PR with Claude and later pushes with Cursor.

## Claude: API vs routine

`claude-mode: api` (default) calls the Anthropic Messages API directly in the runner. `claude-mode:
routine` fires a hosted Claude Code routine instead — set `claude-routine-api-key` and either
`claude-routine-id` or `claude-routine-url`. The PR context, extra context, and approval policy are
sent in the fire request, so the routine needs no manual setup beyond the code-review skill.

## Approval behaviour

- `approval-include` — severities that request changes when left open (default `critical`). Other
  open findings post as a comment; zero open findings approves.
- `approval-disable` — post comments only and skip the verdict and check run.

## Inputs

| Input | Default | Description |
|---|---|---|
| `github-token` | `${{ github.token }}` | Token to read the diff and post reviews/checks |
| `anthropic-api-key` | — | Anthropic key for the Claude API backend |
| `cursor-api-key` | — | Cursor key for the Cursor backend |
| `claude-routine-api-key` | — | Key for firing a hosted Claude routine |
| `claude-routine-id` | — | Routine id (mutually exclusive with `claude-routine-url`) |
| `claude-routine-url` | — | Routine fire URL; the id is parsed from it |
| `review-model` | `auto` | `auto` \| `claude` \| `cursor` |
| `first-review-model` | — | Backend for the first review; empty uses `review-model` |
| `claude-mode` | `api` | `api` \| `routine` |
| `claude-model` | `claude-opus-4-8` | Anthropic model id |
| `cursor-model` | `composer-2.5` | Cursor model id |
| `additional-context` | — | Extra context for the review |
| `approval-include` | `critical` | Severities that request changes when open |
| `approval-disable` | `false` | Comments only; skip the verdict |
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

Or pin an exact release with `@v0.1.0`.

## License

MIT
