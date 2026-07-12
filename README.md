# Code Review Action

A GitHub Action that reviews pull requests with your **Claude Code** or **Codex** subscription and
posts severity-rated inline comments plus an **Approval Verdict** check run. It reviews only changed
lines, caps low-value findings, and reconciles its own threads across pushes.

## Quick start

Add this workflow after creating at least one of the subscription secrets described below:

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
  trust:
    runs-on: ubuntu-latest
    if: >-
      (github.event_name == 'pull_request'
        && github.event.pull_request.head.repo.full_name == github.repository)
      || (github.event_name == 'issue_comment'
        && github.event.issue.pull_request
        && startsWith(github.event.comment.body, 'agent review'))
    outputs:
      trusted: ${{ steps.trust.outputs.trusted }}
    steps:
      - id: trust
        env:
          GH_TOKEN: ${{ github.token }}
          PR_NUMBER: ${{ github.event.pull_request.number || github.event.issue.number }}
        run: |
          head_repo="$(gh api "repos/$GITHUB_REPOSITORY/pulls/$PR_NUMBER" --jq '.head.repo.full_name')"
          echo "trusted=$([[ "$head_repo" == "$GITHUB_REPOSITORY" ]] && echo true || echo false)" >> "$GITHUB_OUTPUT"
  review:
    name: Review
    runs-on: ubuntu-latest
    needs: trust
    if: needs.trust.outputs.trusted == 'true'
    concurrency:
      group: code-review-${{ github.event.pull_request.number || github.event.issue.number }}-${{ github.event_name == 'issue_comment' && 'comment' || 'review' }}
      cancel-in-progress: true
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.sha || format('refs/pull/{0}/head', github.event.issue.number) }}
      - uses: julien777z/code-review-action@v0
        with:
          claude-code-oauth-token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
          codex-auth-json: ${{ secrets.CODEX_AUTH_JSON }}
```

`review-model: auto` prefers Claude and uses Codex when Claude is not configured. When both are
configured, reaching a provider's subscription usage limit switches the current round once to the
other provider. Comment `agent review` on a PR to trigger a manual review.

## Create the subscription secrets

The commands below use the GitHub CLI in the repository whose Actions secrets you want to set.

### Claude Code

Install Claude Code, sign in to the Claude subscription you want to use, then run:

```bash
CLAUDE_TOKEN="$(
  claude setup-token 2>&1 \
    | sed -nE 's/.*(sk-ant-oat01-[[:alnum:]_-]+).*/\1/p' \
    | tail -n 1
)"
test -n "$CLAUDE_TOKEN"
printf '%s' "$CLAUDE_TOKEN" | gh secret set CLAUDE_CODE_OAUTH_TOKEN
unset CLAUDE_TOKEN
```

`claude setup-token` opens an OAuth flow and prints a one-year inference token. Its current output
labels the credential as `Your OAuth token (valid for 1 year)`; the pipeline extracts only the
`sk-ant-oat01-...` value, verifies that parsing succeeded, and sends it to GitHub without putting the
token in shell history.

### Codex

Install Codex, sign in with ChatGPT, validate the resulting credential bundle, and upload the JSON
file directly without printing it:

```bash
codex login
AUTH_FILE="${CODEX_HOME:-$HOME/.codex}/auth.json"
jq -e '.auth_mode == "chatgpt" and ((.tokens.refresh_token // "") != "")' "$AUTH_FILE" >/dev/null
gh secret set CODEX_AUTH_JSON < "$AUTH_FILE"
unset AUTH_FILE
```

The action reconstructs a permission-restricted temporary `CODEX_HOME` on the runner and starts
`codex app-server`. Treat `CODEX_AUTH_JSON` like a password. Account-authenticated Codex automation
is intended for trusted private workflows; do not expose it to forked or untrusted jobs. If Codex can
no longer refresh the copied credentials, rerun the commands above to reseed the secret.

## Provider behavior

- `review-model` — `auto` (default), `claude`, or `codex`.
- `claude-model` — defaults to `claude-opus-4-8`.
- `codex-model` — defaults to `gpt-5.6-terra`; Codex runs it with high reasoning.
- `fallback-on-usage-limit` — defaults to `true`. A structured subscription-limit error switches
  once to the other configured provider, including when an explicit provider was selected.

Fallback preserves the original review timeout. Before the replacement agent starts, the runner
refreshes the action's existing PR comments and includes those findings in the normal prior-findings
block. It supplements that block only with findings emitted in the current round that were not
visible in PR comments, and tells the replacement which provider exhausted its usage. The new agent
then re-evaluates the full diff and re-emits every still-valid finding with the existing title and
severity so thread reconciliation remains correct.

Both providers run in the checked-out repository. Private repositories work through
`actions/checkout` and the workflow's normal `contents: read` permission; no provider-side clone or
separate repository credential is needed. Forked PRs are rejected before subscription credentials
are used.

## Enforcing project rules

With `enforce-project-rules` enabled (the default), the review applies the repository's own coding
rules and reports violations on changed lines. Check out the exact PR head before invoking the
action so Claude Code and Codex can load the applicable project files.

- `enforce-project-rules` — set to `false` to skip repository-rule enforcement.
- `project-rules-severity` — pin rule violations to `critical`, `high`, `medium`, or `low`; empty
  lets the reviewer choose.

## Suggesting simplifications

- `simplify-suggest` — ask a dedicated review sub-agent for optional simplification findings.
- `simplify-suggest-severity` — severity for those suggestions; empty defaults to `low`.
- `simplify-nearby-code` — weigh nearby and related code while still anchoring findings to changed
  lines.

## Approval behavior

- `approval-include` — severities that request changes when open (default `critical`).
- `approval-disable` — post comments only and skip the verdict check.

## Resolving the action's own threads

The action resolves its stale review threads as a PR evolves. GitHub's default workflow token cannot
resolve review threads, so `resolve-token` accepts a GitHub App installation token or fine-grained
PAT with pull-request write permission. It is used only for thread resolution.

```yaml
- uses: actions/create-github-app-token@v3
  id: app-token
  with:
    client-id: ${{ vars.CODE_REVIEW_APP_CLIENT_ID }}
    private-key: ${{ secrets.CODE_REVIEW_APP_PRIVATE_KEY }}
- uses: julien777z/code-review-action@v0
  with:
    claude-code-oauth-token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
    codex-auth-json: ${{ secrets.CODEX_AUTH_JSON }}
    resolve-token: ${{ steps.app-token.outputs.token }}
```

## Inputs

| Input | Default | Description |
|---|---|---|
| `github-token` | `${{ github.token }}` | Token used to read the diff and post reviews/checks |
| `resolve-token` | — | Pull-request-write token used only to resolve review threads |
| `claude-code-oauth-token` | — | Claude subscription token from `claude setup-token` |
| `codex-auth-json` | — | Complete ChatGPT-authenticated Codex `auth.json` |
| `review-model` | `auto` | `auto` \| `claude` \| `codex` |
| `claude-model` | `claude-opus-4-8` | Claude Code model id |
| `codex-model` | `gpt-5.6-terra` | Codex model id; high reasoning is used |
| `fallback-on-usage-limit` | `true` | Switch once to the other configured provider on subscription exhaustion |
| `additional-context` | — | Extra reviewer context |
| `approval-include` | `critical` | Severities that request changes when open |
| `approval-disable` | `false` | Skip the approval verdict and check run |
| `pr-review-summary` | `true` | Add an AI summary to the PR description on open/ready events |
| `enforce-project-rules` | `true` | Enforce repository rules |
| `project-rules-severity` | — | Fixed severity for rule violations |
| `simplify-suggest` | `false` | Suggest code simplifications |
| `simplify-suggest-severity` | — | Severity for simplification suggestions |
| `simplify-nearby-code` | `false` | Weigh nearby/related code for simplifications |
| `min-severity` | `low` | Lowest severity worth posting |
| `low-findings-cap` | `3` | Maximum low-severity findings per review |
| `max-findings` | — | Overall comment cap; empty is uncapped |
| `include-paths` | — | Globs restricting reviewed paths |
| `exclude-paths` | — | Globs excluding paths |
| `trigger-phrase` | `agent review` | Manual-review comment prefix |
| `review-drafts` | `true` | Review draft PRs |
| `pr-number` | — | PR number for `workflow_dispatch` |
| `review-timeout-minutes` | `15` | Review runtime cap; `0` disables it |

## Versioning

Pin the moving major tag for automatic patches:

```yaml
- uses: julien777z/code-review-action@v0
```

## License

MIT
