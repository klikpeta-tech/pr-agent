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

Before deploying a change to `fly/openai-quota-proxy.py`, run its tests (no network or deps needed):

```bash
python scripts/test-openai-quota-proxy.py
```

## One-time setup scripts

```bash
# Create the GitHub App (opens browser, saves creds to ~/.config/klikpeta/pr-agent-credentials.json)
python scripts/create-gh-app.py

# Push secrets to Fly (reads .pem from ~/Downloads/, prompts for OpenAI key)
python scripts/set-fly-secrets.py --pem ~/Downloads/klikpeta-pr-agent.*.pem
```

## Architecture

The Docker image runs **three processes** via `fly/entrypoint.sh`:

- **openai-quota-proxy** (`port 3002`, localhost only) — meters OpenAI free-tier usage
- **pr-agent** (`port 3001`, internal) — the upstream PR-Agent webhook server
- **auto-approve-proxy** (`port 3000`, public) — thin Python reverse proxy

The auto-approve proxy forwards all webhook traffic to pr-agent, then inspects `issue_comment` events. When a bot comment contains `"No major issues detected"`, it fires a GitHub PR approval in a background thread.

**Startup order matters.** `entrypoint.sh` starts each process only after the previous one is accepting connections, polled with `wait_for_port` (fatal if a process dies or never binds). The public port 3000 must stay closed until pr-agent is actually serving: pr-agent takes ~10s to bind, and if the proxy accepts traffic before then, the request gets a 502 instead of Fly holding the connection until the app is ready. That matters because the machine is normally suspended, so a GitHub webhook is what wakes it — and a 502 is a failed delivery. Note this only affects genuine cold boots (after a deploy or an explicit stop, ~20s); the usual idle wake is a suspend/resume that restores memory with all three ports already bound, in well under a second.

**Approval auth priority:** `GITHUB__BOT_PAT` (human PAT, counts toward branch protection) → GitHub App installation token fallback.

### OpenAI free-tier metering

pr-agent sends every LLM call to the quota proxy via `[openai] api_base` in the override TOML. The proxy forwards to `api.openai.com`, reads `usage.total_tokens` off each response, and keeps per-day counters for the two free-grant buckets (tier 1 ≈250K tokens/day for `gpt-5.4` etc., tier 2 ≈2.5M/day for the `-mini`/`-nano` variants). Once tier 1 is spent it rewrites the request's `model` to the tier-2 sibling; once tier 2 is spent too it returns 429 instead of letting the call be billed.

Two non-obvious reasons it works this way:

- **pr-agent's `fallback_models` cannot do this job.** Overage isn't refused by OpenAI, it's billed — so the primary model keeps returning 200 and no fallback ever fires.
- **An org-level enforced spend limit cannot either.** It's all-or-nothing: once tripped, *every* model 429s at once, so falling back to a mini model fails as well. (This is what took the app fully down on 2026-07-31.) Keep a small non-zero spend limit as a backstop against bugs in the proxy's own counting — just don't rely on it to pick models.

Counters live in a JSON file on the `pr_agent_data` Fly volume, mounted at `/data` and pointed at by `QUOTA_STATE_PATH` in `fly.pr-agent.toml`. The volume is what makes the counters survive `fly deploy` — in `/tmp` (the code's default, still used for local runs) they reset on every image change, handing the proxy a full tier-1 budget it hasn't actually spent. Note that attaching the volume pins the app to one machine in `sin`, and adding or removing the mount replaces the machine rather than updating it in place.

Budget is checked against `used + reserved`: a request reserves an estimated cost (prompt length plus the completion cap) while in flight and settles to actual usage afterwards, so parallel requests can't each be told the same budget is free. `QUOTA_HEADROOM` (default `0.90`) is a second margin on top, since a response can still cost more than its estimate. Tune budgets, headroom, and reservation sizing with the env vars documented at the top of `fly/openai-quota-proxy.py`.

One known gap: a streamed response only carries token usage when the request sets `stream_options.include_usage`. pr-agent only streams for `STREAMING_REQUIRED_MODELS` (currently just `openai/qwq-plus`), so nothing streams today, but if that changes the proxy logs a loud "NOT metered" warning rather than silently undercounting.

Current usage is logged to stdout on every call (`fly logs`), and served as JSON from `http://127.0.0.1:3002/__quota` inside the machine.

## Key files

| File | Purpose |
|---|---|
| `fly/entrypoint.sh` | Starts all three processes; kills container if any dies |
| `fly/auto-approve-proxy.py` | Reverse proxy + auto-approve logic |
| `fly/openai-quota-proxy.py` | Meters OpenAI free-tier tokens; downgrades tier 1 → tier 2, then hard-stops |
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
