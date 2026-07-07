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

## PR description summary

On the first review of a PR (opened or marked ready for review), the action appends an AI-generated
summary — a short bullet list of the change, a risk note, and an overview — to the PR description.
Whatever the author wrote stays at the top; the summary is added below it, between hidden markers so a
later review replaces the section in place instead of stacking a second one. It runs only on those
first-review events, never on later pushes.

- `pr-review-summary` — set to `false` to turn the summary off (default `true`).

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
The action creates a cloud environment for each run and deletes it afterward; to reuse a pre-provisioned
environment instead, set `claude-environment-id`. When `enforce-project-rules` is `false`, the session
runs without the repository mounted and reviews from the diff alone.

- `enforce-project-rules` — set to `false` to skip loading and enforcing the repo's rules (default
  `true`).
- `project-rules-severity` — pin every rule violation to a fixed severity (`critical`, `high`,
  `medium`, or `low`). Empty lets the review rate each violation itself (default empty). Set this above
  `low` when rule violations are being crowded out by `low-findings-cap`.
- `claude-environment-id` — Managed Agents cloud environment id the Claude backend reuses instead of
  creating a fresh one per run. Empty creates and deletes one each run (default empty).

## Suggesting simplifications

Off by default, the review can also suggest code simplifications as low-severity nits (optional
suggestions that never block).

- `simplify-suggest` — apply the agent's `code-simplify` skill to the changed code and suggest
  simplifications (default `false`).
- `simplify-nearby-code` — extend those suggestions to weigh the nearby and related code the change
  touches, not just the changed lines in isolation (default `false`). Findings still anchor on changed
  lines.

## Approval behaviour

- `approval-include` — severities that request changes when left open (default `critical`). Other
  open findings post as a comment; zero open findings approves.
- `approval-disable` — post comments only and skip the verdict and check run.

## Resolving the action's own threads

As the PR evolves the action resolves the review threads whose findings no longer apply. The default
`GITHUB_TOKEN` **cannot** resolve review threads — GitHub rejects `resolveReviewThread` with "Resource
not accessible by integration" — so by default stale threads stay open even though the verdict count is
correct.

To enable auto-resolution, give the action a token with pull-request write via `resolve-token`. It is
used **only** to resolve threads, so review comments stay authored by `github-actions[bot]`.

A **GitHub App** is the best fit across several repos: create it once, install it on each repo, and the
same workflow snippet below mints a per-repo token automatically. The minted token is short-lived — it
expires after an hour and is revoked when the job ends — so nothing long-lived is stored. The App is
yours and lives in your account; no server to run.

1. Create a GitHub App (**Settings → Developer settings → GitHub Apps → New GitHub App**). Under
   **Permissions → Repository → Pull requests** select **Read and write**; leave everything else off.
2. **Install** the App on every repository whose threads it should resolve.
3. On the App's settings page, note its **Client ID** and **Generate a private key** (downloads a `.pem`).
4. Expose the credentials to those repos' workflows:
   - **Organization repos:** add an organization **variable** `CODE_REVIEW_APP_CLIENT_ID` and
     organization **secret** `CODE_REVIEW_APP_PRIVATE_KEY` once, scoped to the repos that use the action.
   - **Personal repos:** add the same **variable** and **secret** to each repo (personal accounts have no
     secrets shared across repos).
5. Mint a token in the workflow and pass it to `resolve-token`:

```yaml
steps:
  - uses: actions/create-github-app-token@v3
    id: app-token
    with:
      client-id: ${{ vars.CODE_REVIEW_APP_CLIENT_ID }}
      private-key: ${{ secrets.CODE_REVIEW_APP_PRIVATE_KEY }}
  - uses: julien777z/code-review-action@v0
    with:
      cursor-api-key: ${{ secrets.CURSOR_API_KEY }}
      resolve-token: ${{ steps.app-token.outputs.token }}
```

For a single repository, a fine-grained PAT with **Pull requests: write** stored as the
`CODE_REVIEW_TOKEN` secret also works (`resolve-token: ${{ secrets.CODE_REVIEW_TOKEN }}`), but it is
tied to your account and expires, so the App scales better.

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
| `resolve-token` | — | Token to resolve the action's own threads; needs a GitHub App token (see above) |
| `anthropic-api-key` | — | Anthropic key for the Claude API backend |
| `cursor-api-key` | — | Cursor key for the Cursor backend |
| `review-model` | `auto` | `auto` \| `claude` \| `cursor` |
| `first-review-model` | — | Backend for the first review; empty uses `review-model` |
| `claude-model` | `claude-opus-4-8` | Anthropic model id |
| `cursor-model` | `composer-2.5` | Cursor model id |
| `claude-environment-id` | — | Managed Agents environment the Claude backend reuses; empty creates one per run |
| `additional-context` | — | Extra context for the review |
| `approval-include` | `critical` | Severities that request changes when open |
| `approval-disable` | `false` | Comments only; skip the verdict |
| `pr-review-summary` | `true` | Append an AI summary to the PR description on the first review |
| `enforce-project-rules` | `true` | Enforce the repository's own coding rules; no-op when it defines none |
| `project-rules-severity` | — | Fixed severity for rule violations; empty lets the review rate each |
| `simplify-suggest` | `false` | Suggest code simplifications as low-severity nits |
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
