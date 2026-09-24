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

Before deploying, run the tests for whichever proxy you changed (no network or deps needed):

```bash
python scripts/test-openai-quota-proxy.py
python scripts/test-auto-approve-proxy.py
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

The auto-approve proxy forwards all webhook traffic to pr-agent, then inspects `issue_comment` events. When pr-agent's review lists no focus areas, it fires a GitHub PR approval in a background thread; otherwise it submits REQUEST_CHANGES.

**How "clean" is detected.** pr-agent renders a clean `key_issues_to_review` in one of two distinct shapes (confirmed by reading `pr_agent/algo/utils.py` in the `pragent/pr-agent:latest` image, around the `is_value_no(value)` branch), and `review_is_clean()` recognizes both as positive evidence:

1. The model returned no findings at all → a dedicated row: `<tr><td>{emoji}&nbsp;<strong>No major issues detected</strong></td></tr>`.
2. The model returned findings that all got filtered out while rendering → the "Recommended focus areas for review" heading stays, but its cell is left empty.

If the markup ever changes shape and matches neither, the check returns False and the PR gets REQUEST_CHANGES, which is the safe direction: wrongly guessing "clean" would auto-approve a PR with real findings, and these approvals count toward branch protection.

Do not go back to matching the bare prose phrase "No major issues detected" anywhere in the body. The proxy originally did that, and it never matched — upstream only writes that string (lowercased) as an internal reason for *withholding* a review, never in the published comment — so every review took the request-changes branch and no PR was ever auto-approved. Shape 1 above looks similar but is a different, structural check (exact row markup), added back deliberately after confirming the real rendering.

**Startup order matters.** `entrypoint.sh` starts each process only after the previous one is accepting connections, polled with `wait_for_port` (fatal if a process dies or never binds). The public port 3000 must stay closed until pr-agent is actually serving: pr-agent takes ~10s to bind, and if the proxy accepts traffic before then, the request gets a 502 instead of Fly holding the connection until the app is ready. That matters because the machine is normally suspended, so a GitHub webhook is what wakes it — and a 502 is a failed delivery. Note this only affects genuine cold boots (after a deploy or an explicit stop, ~20s); the usual idle wake is a suspend/resume that restores memory with all three ports already bound, in well under a second.

**A hung process isn't a dead process.** `entrypoint.sh`'s own self-healing (`wait_any` in the loop at the bottom) only detects a process *exiting* (`kill -0`); it has no way to notice one that's alive but wedged — stuck on a slow or hanging upstream call, for example. That happened for real on 2026-09-24: pr-agent stopped answering any request at all while Fly still reported the machine as "started", and nothing restarted it until a manual `fly apps restart`. `fly.pr-agent.toml` now has an `[[http_service.checks]]` block (`GET /`, which auto-approve-proxy forwards straight through to pr-agent on :3001, so the check exercises the same path real traffic takes) so Fly's own platform-level health checking can catch and restart this automatically. That check subsystem is separate from the `http_service` idle-connection tracking that drives `auto_stop_machines`, so it shouldn't stop the app from suspending when genuinely idle — but that interaction is worth re-verifying (`fly status` should still show `suspended` after a few idle minutes, not stuck at `started`) if the check config ever changes.

**Approval auth priority:** `GITHUB__BOT_PAT` (human PAT, counts toward branch protection) → GitHub App installation token fallback.

### OpenAI free-tier metering, with DeepSeek as the overflow for the big-model budget

pr-agent sends every LLM call to the quota proxy via `[openai] api_base` in the override TOML. The proxy tracks three separate buckets:

- **`openai`** (~250K tokens/day: `gpt-5.4` etc., what `config.model` uses) — the OpenAI free grant, forwarded to `api.openai.com`. Once spent, the proxy rewrites the request's `model` to `QUOTA_DEEPSEEK_MODEL` (default `deepseek-v4-flash`) and reroutes it to `api.deepseek.com` instead — a different upstream, with `DEEPSEEK__KEY` injected as the proxy's own Authorization header rather than forwarding the caller's OpenAI key.
- **`openai_mini`** (~2.5M tokens/day: `gpt-5.4-mini` etc., what `config.model_weak` uses) — a genuinely separate OpenAI free grant from `openai`, tracked and hard-stopped (429) independently. There's no in-proxy downgrade target for this bucket: a 429 here is a real failure pr-agent sees, and its own `fallback_models` list (`["gpt-5.4-mini", "deepseek/deepseek-v4-flash"]`) is what actually resolves it, falling through to a direct DeepSeek call outside this proxy.
- **`deepseek`** — the overflow destination for an exhausted `openai` bucket, not a free grant at all. It bills per token from the first call, so its budget (`DEEPSEEK_DAILY_TOKENS`, default 20M tokens/day) is a generous runaway-spend circuit breaker, not something to stay under by design.

If `DEEPSEEK__KEY` isn't set, or the `deepseek` circuit-breaker cap is somehow spent too, the proxy returns 429 rather than let anything through unmetered.

Two non-obvious reasons the `openai` switch is proactive rather than error-driven:

- **pr-agent's `fallback_models` cannot do this job on its own.** OpenAI overage isn't refused, it's billed — so the primary model keeps returning 200 and no fallback ever fires. This is exactly why the proxy has to manufacture the "budget spent" signal itself for the `openai` bucket; pr-agent has no other way to see it. (`openai_mini` is different: OpenAI does eventually refuse that grant with a real error once *its* limit is hit, which is what lets `fallback_models` react to it at all.)
- **An org-level enforced spend limit cannot either.** It's all-or-nothing: once tripped, *every* OpenAI model 429s at once. (This is what took the app fully down on 2026-07-31.) Keep a small non-zero spend limit as a backstop against bugs in the proxy's own counting — just don't rely on it to pick models.

Counters live in a JSON file on the `pr_agent_data` Fly volume, mounted at `/data` and pointed at by `QUOTA_STATE_PATH` in `fly.pr-agent.toml`. The volume is what makes the counters survive `fly deploy` — in `/tmp` (the code's default, still used for local runs) they reset on every image change, handing the proxy a full `openai` budget it hasn't actually spent. Note that attaching the volume pins the app to one machine in `sin`, and adding or removing the mount replaces the machine rather than updating it in place.

Budget is checked against `used + reserved` per bucket: a request reserves an estimated cost (prompt length plus the completion cap) while in flight and settles to actual usage afterwards, so parallel requests can't each be told the same budget is free. `QUOTA_HEADROOM` (default `0.90`) is a second margin on top, since a response can still cost more than its estimate. Tune budgets, headroom, and reservation sizing with the env vars documented at the top of `fly/openai-quota-proxy.py`.

A streamed response only carries token usage when the request sets `stream_options.include_usage`, and pr-agent only streams for `STREAMING_REQUIRED_MODELS` (currently just `openai/qwq-plus`), so nothing streams today. If a successful call's usage can't be read — a stream with no usage chunk, or any 200 missing the field — the proxy charges the pre-flight estimate instead of zero and logs why. That matters because a free ride is unbounded: repeating an unreadable request would otherwise walk straight past a free-grant cap or the DeepSeek circuit breaker. Over-charging only under-uses the budget; under-charging is what produces a surprise bill (`openai`) or hides real spend (`deepseek`). Errors are never charged, since neither provider bills them.

Current usage is logged to stdout on every call (`fly logs`), and served as JSON from `http://127.0.0.1:3002/__quota` inside the machine.

**DeepSeek latency, `config.ai_timeout`, and `retry_same_model_on_timeout`.** pr-agent's upstream default `ai_timeout` (120s) is sized for OpenAI. DeepSeek (`deepseek-v4-flash`, thinking mode) has been observed taking well over that on a large diff. `pr-agent-override.toml` sets `ai_timeout = 300` to match the quota proxy's own `QUOTA_UPSTREAM_TIMEOUT`, so neither side gives up before the other — but that alone wasn't enough: `retry_same_model_on_timeout` defaults to `true`, and since `config.model` (`gpt-5.4`) is often transparently downgraded to the slower DeepSeek overflow by the proxy, a timeout there just repeats the same slow call instead of moving on, and a review can silently burn 15+ minutes across retries without ever posting a result (observed for real on 2026-09-23/24, including on automatic push-triggered reviews with nothing to retry them). The override sets `retry_same_model_on_timeout = false` so one timeout moves straight to `fallback_models[0] = gpt-5.4-mini` — a normally-fast real OpenAI call against the separate, usually-underused `openai_mini` budget — instead of hammering the same slow path.

**Why DeepSeek is slow in the first place: `additional_reasoning_effort_models`.** The timeout tuning above treats the symptom; the actual cause is that DeepSeek's own API defaults `deepseek-v4-flash` to thinking mode at reasoning effort `"high"` (api-docs.deepseek.com/guides/thinking_mode). pr-agent already has a `config.reasoning_effort` default (`"medium"`, set upstream) meant to rein exactly this in, but it only auto-applies to models litellm's bundled metadata already recognizes as reasoning-capable — `deepseek-v4-flash` is too new for that, the same reason it's absent from pr-agent's own `MAX_TOKENS` table (see `custom_model_max_tokens`). `additional_reasoning_effort_models = ["deepseek/deepseek-v4-flash"]` is pr-agent's documented escape hatch for this exact gap: it makes DeepSeek receive `config.reasoning_effort` as a real `reasoning_effort` kwarg (litellm forwards it, and DeepSeek's API accepts that same parameter name) instead of silently falling back to DeepSeek's own high-effort default.

## Key files

| File | Purpose |
|---|---|
| `fly/entrypoint.sh` | Starts all three processes; kills container if any dies |
| `fly/auto-approve-proxy.py` | Reverse proxy + auto-approve logic |
| `fly/openai-quota-proxy.py` | Meters the OpenAI `openai`/`openai_mini` free grants separately; reroutes `openai` to DeepSeek once spent, then hard-stops |
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
- `DEEPSEEK__KEY` — used two ways: (1) directly by pr-agent, whenever `fallback_models` falls through to `deepseek/deepseek-v4-flash` (e.g. after `openai_mini` is exhausted), bypassing the quota proxy entirely; (2) by `fly/openai-quota-proxy.py` itself, which injects it for the `deepseek` bucket — the overflow destination once the `openai` free grant is spent. Both bill straight to the DeepSeek account; the proxy's `deepseek` bucket has a cap, but it's a generous circuit breaker, not a real budget.
- `GITHUB__BOT_PAT` — human PAT for approvals that count toward branch protection rules
