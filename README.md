# klikpeta-tech/pr-agent

PR-Agent self-hosted GitHub App deployed on Fly.io, providing automated code review for all repositories under the **klikpeta-tech** org — no per-repo workflow files needed.

## How it works

A single webhook server receives GitHub events from every repo in the org (via the GitHub App installation). On each PR open / push:

1. **`/describe`** — generates an AI PR title and description
2. **`/review`** — posts a code review with findings
3. **`/improve`** — suggests inline code improvements

When the review passes ("No major issues detected"), the server automatically submits a formal **Approve** review, satisfying branch protection rules.

## Architecture

```
GitHub App (installed org-wide on klikpeta-tech)
  ↓ webhooks
auto-approve-proxy (port 3000)  ← public Fly.io endpoint
  ↓ forwards everything
pr-agent server      (port 3001) ← internal only
  ↓ GitHub API
PR comments + approvals
```

The `auto-approve-proxy.py` is a thin reverse proxy that forwards all webhook traffic to pr-agent and intercepts `issue_comment` events from the bot to trigger approvals.

## Deployment

### 1. Prerequisites

- [flyctl](https://fly.io/docs/hands-on/install-flyctl/) installed and authenticated
- GitHub App created at `https://github.com/organizations/klikpeta-tech/settings/apps`
  - Required permissions: Pull requests (R/W), Issue comments (R/W), Metadata (R), Contents (R)
  - Subscribed events: Issue comment, Pull request, Push
  - Webhook URL: `https://klikpeta-pr-agent.fly.dev/api/v1/github_webhooks`

### 2. Create the GitHub App (automated)

```bash
python scripts/create-gh-app.py
# Opens browser → completes GitHub App manifest flow → saves credentials locally
```

### 3. Set Fly secrets

```bash
python scripts/set-fly-secrets.py --pem ~/Downloads/klikpeta-pr-agent.*.pem
# Prompts for OpenAI key, then sets all secrets on the Fly app
```

Reference: [`fly/pr-agent.secrets.example`](fly/pr-agent.secrets.example)

### 4. Deploy

```bash
fly deploy --config fly/fly.pr-agent.toml --app klikpeta-pr-agent
```

### 5. Install the GitHub App org-wide

```
https://github.com/apps/klikpeta-pr-agent/installations/new
```
Select **klikpeta-tech** → **All repositories**.

## Configuration

### Server config — `fly/pr-agent-override.toml`

Baked into the Docker image (pr-agent uses dynaconf, env vars alone are not reliable).

### Org-wide config — `fly/pr-agent.org-config.toml`

Copy to the `klikpeta-tech/.github` repo as `.pr_agent.toml`. Applies to all repos automatically.

## Interactive commands

Comment on any PR (must start with `/`):

| Command | Effect |
|---|---|
| `/review` | Re-run code review |
| `/improve` | Re-run code suggestions |
| `/describe` | Re-generate PR description |
| `/ask <question>` | Ask a question about the PR |

## Docs

- [PR-Agent docs](https://pr-agent-docs.codium.ai/)
- [GitHub App installation guide](https://pr-agent-docs.codium.ai/installation/github/#run-as-a-github-app)
