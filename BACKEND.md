# Webhook backend

A self-hosted webhook service that runs the same review engine as the action, but reacts to GitHub App
webhooks instead of running in each consumer's CI. Reviews post under **your GitHub App's identity** and
the App resolves its own threads, so consumers only **install the App** — no workflow, no secrets, and a
single App-owned check on the PR (no GitHub Actions job check).

It processes **one review at a time** (a single in-process worker), which keeps the shared engine simple
and avoids hammering a model or the GitHub API. For higher throughput, put a real queue in front of it.

## How it works

1. GitHub delivers a webhook (`pull_request` or `issue_comment`) to `POST /webhooks/github`.
2. The service verifies the `X-Hub-Signature-256` HMAC against `GITHUB_WEBHOOK_SECRET` and drops
   ineligible deliveries (bots, forks, wrong trigger phrase).
3. It mints a short-lived **installation token** for the delivering installation (App JWT →
   `POST /app/installations/{id}/access_tokens`) and runs one review round under that token.
4. `GET /health` reports liveness for container health checks.

## Create the GitHub App

1. **Settings → Developer settings → GitHub Apps → New GitHub App.**
2. **Webhook:** set the URL to `https://<your-host>/webhooks/github` and a strong **secret** (this becomes
   `GITHUB_WEBHOOK_SECRET`).
3. **Permissions → Repository:** **Pull requests: Read and write** and **Checks: Read and write**.
   (Optionally **Issues: Read and write** for the 👀 reaction; it degrades gracefully without it.)
4. **Subscribe to events:** **Pull request** and **Issue comment**.
5. **Install** the App on the repositories it should review.
6. Note the **App ID** and **Generate a private key** (downloads a `.pem`).

## Configuration (environment)

| Variable | Required | Description |
|---|---|---|
| `GITHUB_APP_ID` | yes | The App's ID (used as the App JWT issuer) |
| `GITHUB_APP_PRIVATE_KEY` | yes | The App private key PEM (with newlines) |
| `GITHUB_WEBHOOK_SECRET` | yes | The webhook secret configured on the App |
| `ANTHROPIC_API_KEY` / `CURSOR_API_KEY` | one | Backend credential the service funds reviews with |
| `PORT` | no | Listen port (default `8080`) |
| `REVIEW_MODEL`, `MIN_SEVERITY`, `APPROVAL_INCLUDE`, … | no | Same review settings as the action's inputs, as env vars |

## Build and run

```bash
docker build -t code-review-backend .

docker run -p 8080:8080 \
  -e GITHUB_APP_ID=123456 \
  -e GITHUB_APP_PRIVATE_KEY="$(cat app-private-key.pem)" \
  -e GITHUB_WEBHOOK_SECRET=your-webhook-secret \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  code-review-backend
```

The container must be reachable by GitHub over **public HTTPS** — put it behind a reverse proxy
(Caddy/nginx) that terminates TLS, or use a tunnel (`cloudflared`, ngrok) while testing.

## Notes

- The private key is the trust anchor: the App has write access on every repo it is installed on, so keep
  the key in a real secret store and rotate it if exposed.
- The Claude (Anthropic) backend runs out of the box. The Cursor backend depends on whatever
  `cursor-sdk` needs at runtime; verify it in the container if you use Cursor.
- One review runs at a time; bursts queue in memory and are processed in order.
