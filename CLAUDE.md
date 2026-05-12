# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Infrastructure config and tooling for a self-hosted [PR-Agent](https://pr-agent-docs.codium.ai/) GitHub App running on Fly.io. It provides automated code review (describe, review, improve) for all repos in the **klikpeta-tech** GitHub org. There is no application source code here — the `pragent/pr-agent:latest` Docker image is the upstream.

## Deployment

```bash
# Deploy the app
fly deploy --config fly/fly.pr-agent.toml --app klikpeta-pr-agent

# View logs
fly logs --app klikpeta-pr-agent

# SSH into the running machine
fly ssh console --app klikpeta-pr-agent
```

## One-time setup scripts

```bash
# Create the GitHub App (opens browser, saves creds to ~/.config/klikpeta/pr-agent-credentials.json)
python scripts/create-gh-app.py

# Push secrets to Fly (reads .pem from ~/Downloads/, prompts for OpenAI key)
python scripts/set-fly-secrets.py --pem ~/Downloads/klikpeta-pr-agent.*.pem
```

## Architecture

The Docker image runs **two processes** via `fly/entrypoint.sh`:

- **pr-agent** (`port 3001`, internal) — the upstream PR-Agent webhook server
- **auto-approve-proxy** (`port 3000`, public) — thin Python reverse proxy

The proxy forwards all webhook traffic to pr-agent, then inspects `issue_comment` events. When a bot comment contains `"No major issues detected"`, it fires a GitHub PR approval in a background thread.

**Approval auth priority:** `GITHUB__BOT_PAT` (human PAT, counts toward branch protection) → GitHub App installation token fallback.

## Key files

| File | Purpose |
|---|---|
| `fly/entrypoint.sh` | Starts both processes; kills container if either dies |
| `fly/auto-approve-proxy.py` | Reverse proxy + auto-approve logic |
| `fly/pr-agent.Dockerfile` | Builds image from `pragent/pr-agent:latest` |
| `fly/pr-agent-override.toml` | Baked-in pr-agent config (model, triggers, review settings); copied to `/app/pr_agent/settings/.secrets.toml` |
| `fly/pr-agent.org-config.toml` | Org-wide config — deploy to `klikpeta-tech/.github` as `.pr_agent.toml` |
| `fly/fly.pr-agent.toml` | Fly.io app config (region: `sin`, 512 MB, suspend on idle) |
| `fly/pr-agent.secrets.example` | Required env vars reference |

## Configuration split

There are two config layers:

1. **Server config** (`fly/pr-agent-override.toml`) — baked into the Docker image. Controls model, auto-run triggers (`handle_pr_actions`, `push_commands`), and review behavior. Changes require a redeploy.
2. **Org config** (`fly/pr-agent.org-config.toml`) — lives in the `klikpeta-tech/.github` repo as `.pr_agent.toml`. Applied per-repo without a redeploy.

PR-Agent uses dynaconf; env vars alone are not reliable for config, which is why the override TOML is baked in rather than set as Fly secrets.

## Secrets

Fly secrets (set via `fly secrets set --app klikpeta-pr-agent KEY=VALUE`):

- `GITHUB__APP_ID`, `GITHUB__WEBHOOK_SECRET`, `GITHUB__PRIVATE_KEY` — GitHub App credentials
- `OPENAI__KEY` — LLM provider (or use `ANTHROPIC__KEY` + set `CONFIG__AI__MODEL`)
- `GITHUB__BOT_PAT` — human PAT for approvals that count toward branch protection rules
