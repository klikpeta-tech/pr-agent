#!/usr/bin/env python3
"""
Quota-aware proxy that sits between pr-agent (litellm) and api.openai.com,
with an overflow lane to DeepSeek once the OpenAI "big model" grant runs out.

Why this exists
---------------
OpenAI's free daily grant has two buckets (see platform docs / the promo notice):

  openai       ~250K tokens/day    gpt-5.4, gpt-5.2, gpt-5.1, gpt-5, gpt-4.1, gpt-4o, o1, o3, ...
  openai_mini  ~2.5M tokens/day    gpt-5.4-mini, gpt-5-mini, gpt-4.1-mini, o3-mini, ...

Usage past those limits is *billed*, not refused, so pr-agent's `fallback_models`
never triggers on quota exhaustion — the primary model keeps returning 200 and
silently spends real credit. Conversely, an org-level enforced spend limit is
all-or-nothing: once tripped it blocks every model at once, so a fallback
can't help there either.

This proxy makes the switch proactive instead of error-driven: it counts the
tokens each response actually consumed, and tracks each bucket's own daily
budget separately, since they're genuinely separate OpenAI grants:

  - `openai` exhausted -> rewrite the request to call DeepSeek instead — a
    different upstream, a different model, and a different API key that this
    proxy injects itself rather than forwarding from the caller (pr-agent only
    ever holds an OpenAI key, so it couldn't authenticate to DeepSeek even if
    it tried).
  - `openai_mini` exhausted -> hard-stop (429), same as it always has. There is
    no in-proxy downgrade target for it: pr-agent's own `fallback_models` list
    already places `deepseek/deepseek-v4-flash` right after the mini model, so
    a mini 429 becomes a genuine exception-driven fallback that pr-agent
    resolves itself, calling DeepSeek directly with its own `[deepseek]` key —
    outside this proxy entirely, since that request carries an explicit
    `deepseek/` provider prefix and never touches `[openai] api_base`.

DeepSeek has no free grant to protect and bills per token from the first
call, so its budget below is a generous circuit-breaker cap against a
metering bug driving runaway spend, not a budget to stay under — the same
philosophy as the small non-zero OpenAI org spend limit kept as a backstop
elsewhere (see CLAUDE.md).

Wiring
------
pr-agent points at this proxy via `[openai] api_base` in pr-agent-override.toml.
The caller's OpenAI Authorization header is forwarded untouched for `openai`
and `openai_mini` calls; `deepseek` calls use this proxy's own DEEPSEEK__KEY
instead.

Environment variables
---------------------
  QUOTA_PROXY_PORT          Port to listen on                          (default 3002)
  OPENAI_UPSTREAM_BASE      Real OpenAI API base                       (default https://api.openai.com)
  DEEPSEEK_UPSTREAM_BASE    Real DeepSeek API base                     (default https://api.deepseek.com)
  DEEPSEEK__KEY             DeepSeek API key used for the overflow lane (shared with pr-agent's own [deepseek] secret)
  QUOTA_DEEPSEEK_MODEL      Model to call once `openai` is spent       (default deepseek-v4-flash)
  QUOTA_STATE_PATH          Where the daily counters live              (default /tmp/openai-quota-state.json)
  OPENAI_DAILY_TOKENS       `openai` (big model) free-grant budget     (default 250000)
  OPENAI_MINI_DAILY_TOKENS  `openai_mini` free-grant budget            (default 2500000)
  DEEPSEEK_DAILY_TOKENS     `deepseek` circuit-breaker cap             (default 20000000)
  QUOTA_HEADROOM            Fraction of budget actually usable         (default 0.90)
  QUOTA_TZ_OFFSET_HOURS     Hour offset for the daily reset            (default 0 = UTC midnight)
  QUOTA_UPSTREAM_TIMEOUT    Upstream request timeout, seconds          (default 300)

  QUOTA_PRESEED_DAY         Date (YYYY-MM-DD) the preseed below applies to
  QUOTA_PRESEED_OPENAI      Tokens to treat as already spent on that day
  QUOTA_PRESEED_OPENAI_MINI Tokens to treat as already spent on that day

The preseed exists because the counters start at zero on a fresh deploy while
OpenAI's real grant may already be partly spent — sending `openai` traffic then
would be billed. It only applies on QUOTA_PRESEED_DAY and only when there is no
saved state for that day, so it expires by itself at the next daily rollover.
"""

import http.server
import json
import os
import re
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

LISTEN_PORT = int(os.environ.get("QUOTA_PROXY_PORT", "3002"))
OPENAI_BASE = os.environ.get("OPENAI_UPSTREAM_BASE", "https://api.openai.com").rstrip("/")
DEEPSEEK_BASE = os.environ.get("DEEPSEEK_UPSTREAM_BASE", "https://api.deepseek.com").rstrip("/")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK__KEY", "")
DEEPSEEK_MODEL = os.environ.get("QUOTA_DEEPSEEK_MODEL", "deepseek-v4-flash")
STATE_PATH = os.environ.get("QUOTA_STATE_PATH", "/tmp/openai-quota-state.json")
UPSTREAM_TIMEOUT = float(os.environ.get("QUOTA_UPSTREAM_TIMEOUT", "300"))
TZ_OFFSET_HOURS = float(os.environ.get("QUOTA_TZ_OFFSET_HOURS", "0"))

PRESEED_DAY = os.environ.get("QUOTA_PRESEED_DAY", "").strip()
PRESEED = {
    "openai": int(os.environ.get("QUOTA_PRESEED_OPENAI", "0")),
    "openai_mini": int(os.environ.get("QUOTA_PRESEED_OPENAI_MINI", "0")),
}

# Only a fraction of each budget is spendable. Token cost is only known *after*
# a response comes back, so the remainder absorbs the request that crosses the
# line — without it a single large diff could overshoot into paid usage
# (openai), past the other free grant (openai_mini), or past the circuit
# breaker (deepseek).
HEADROOM = float(os.environ.get("QUOTA_HEADROOM", "0.90"))

# Each in-flight request reserves an estimated cost against its bucket until
# the real usage arrives. Without a reservation, concurrent requests would all
# read the same remaining balance, pass the check together, and collectively
# overshoot the cap by far more than HEADROOM absorbs.
RESERVE_MIN = int(os.environ.get("QUOTA_RESERVE_MIN", "2000"))
RESERVE_COMPLETION_DEFAULT = int(os.environ.get("QUOTA_RESERVE_COMPLETION", "4000"))

TIER_BUDGET = {
    "openai": int(os.environ.get("OPENAI_DAILY_TOKENS", "250000")),
    "openai_mini": int(os.environ.get("OPENAI_MINI_DAILY_TOKENS", "2500000")),
    "deepseek": int(os.environ.get("DEEPSEEK_DAILY_TOKENS", "20000000")),
}
TIER_SPENDABLE = {tier: int(budget * HEADROOM) for tier, budget in TIER_BUDGET.items()}

OPENAI_MODELS = frozenset(
    [
        "gpt-5.4", "gpt-5.2", "gpt-5.1", "gpt-5.1-codex", "gpt-5",
        "gpt-5-codex", "gpt-5-chat-latest", "gpt-4.1", "gpt-4o", "o1", "o3",
    ]
)
OPENAI_MINI_MODELS = frozenset(
    [
        "gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.1-codex-mini", "gpt-5-mini",
        "gpt-5-nano", "gpt-4.1-mini", "gpt-4.1-nano", "gpt-4o-mini",
        "o1-mini", "o3-mini", "o4-mini", "codex-mini-latest",
    ]
)

_DATE_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")


def normalize_model(name: str) -> str:
    """'openai/gpt-5.4-2026-03-05' -> 'gpt-5.4' so dated snapshots classify."""
    base = name.strip().lower().rsplit("/", 1)[-1]
    return _DATE_SUFFIX.sub("", base)


def classify(name: str) -> str | None:
    """Return 'openai', 'openai_mini', or None when the model isn't tracked.

    There's no such thing as a direct 'deepseek' *request* from the caller's
    side: that bucket is only ever reached by this proxy downgrading an
    'openai' request once its budget is spent, never by the caller asking for
    it by name (a literal `deepseek/...` request bypasses this proxy's
    `[openai] api_base` entirely and never reaches here).
    """
    norm = normalize_model(name)
    if norm in OPENAI_MINI_MODELS:  # checked first: 'gpt-5.4-mini' also prefixes 'gpt-5.4'
        return "openai_mini"
    if norm in OPENAI_MODELS:
        return "openai"
    return None


def current_day() -> str:
    tz = timezone(timedelta(hours=TZ_OFFSET_HOURS))
    return datetime.now(tz).strftime("%Y-%m-%d")


# ── Daily counters ────────────────────────────────────────────────────────────

def estimate_request_tokens(payload: dict, raw_len: int) -> int:
    """Pre-flight cost estimate used to reserve budget before the real number is
    known: ~4 characters per token for the prompt, plus the completion cap."""
    completion = (
        payload.get("max_completion_tokens")
        or payload.get("max_tokens")
        or RESERVE_COMPLETION_DEFAULT
    )
    try:
        completion = int(completion)
    except (TypeError, ValueError):
        completion = RESERVE_COMPLETION_DEFAULT
    return max(RESERVE_MIN, raw_len // 4 + completion)


class QuotaState:
    """Token counters for the current day, persisted so restarts don't reset them.

    Budget is checked against used + reserved, and a request reserves its
    estimated cost while in flight, so parallel requests can't each be told the
    same budget is free. Only `used` is persisted — reservations are in-flight
    state with no meaning across a restart.
    """

    TIERS = ("openai", "openai_mini", "deepseek")

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._day = current_day()
        self._used = {t: 0 for t in self.TIERS}
        self._reserved = {t: 0 for t in self.TIERS}
        self._load()

    def _committed_locked(self, tier: str) -> int:
        return self._used[tier] + self._reserved[tier]

    def _apply_preseed(self) -> None:
        """Treat part of today's grant as already spent (see QUOTA_PRESEED_DAY)."""
        if not PRESEED_DAY or PRESEED_DAY != self._day:
            return
        if not any(PRESEED.values()):
            return
        self._used["openai"] = PRESEED["openai"]
        self._used["openai_mini"] = PRESEED["openai_mini"]
        print(
            f"[quota] Preseeded {self._day} as already spent:"
            f" openai={self._used['openai']:,} openai_mini={self._used['openai_mini']:,}",
            flush=True,
        )

    def _load(self) -> None:
        try:
            with open(self.path) as fh:
                data = json.load(fh)
        except FileNotFoundError:
            self._apply_preseed()
            return
        except Exception as exc:
            print(f"[quota] ⚠️  Could not read state {self.path}: {exc}", flush=True)
            self._apply_preseed()
            return
        if data.get("day") != self._day:
            print(
                f"[quota] State file is from {data.get('day')}, today is {self._day}"
                " — starting fresh.",
                flush=True,
            )
            self._apply_preseed()
            return
        for tier in self.TIERS:
            self._used[tier] = int(data.get(tier, 0))
        print(
            f"[quota] Resumed {self._day}: "
            + " ".join(f"{t}={self._used[t]:,}" for t in self.TIERS),
            flush=True,
        )

    def _save_locked(self) -> None:
        payload = {"day": self._day, **{t: self._used[t] for t in self.TIERS}}
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump(payload, fh)
            os.replace(tmp, self.path)  # atomic, so a crash can't truncate the file
        except Exception as exc:
            print(f"[quota] ⚠️  Could not persist state: {exc}", flush=True)

    def _roll_day_locked(self) -> None:
        today = current_day()
        if today != self._day:
            spent = " ".join(f"{t}={self._used[t]:,}" for t in self.TIERS)
            print(f"[quota] 🔄 New day {today} — resetting (spent {spent} on {self._day})", flush=True)
            self._day = today
            self._used = {t: 0 for t in self.TIERS}
            self._save_locked()

    def choose_model(self, requested: str, estimate: int) -> tuple[str, str | None, str, int]:
        """Pick the model (and, implicitly, the upstream) to actually call.

        Returns (model, tier_to_charge, action, reserved) where action is one
        of 'as_is', 'deepseek', 'exhausted', 'untracked'. Every non-zero
        `reserved` must be handed back to settle() exactly once.

        'deepseek' means route to DEEPSEEK_BASE with DEEPSEEK_API_KEY instead
        of OpenAI — only reachable by downgrading an exhausted 'openai'
        request, never a direct classification. 'exhausted' covers three
        distinct cases the caller distinguishes for logging: the openai_mini
        budget is spent (hard stop, no downgrade target — matches the
        pre-DeepSeek behavior), DEEPSEEK__KEY isn't configured (so attempting
        the overflow would just 401), or DeepSeek's own circuit-breaker cap is
        somehow spent too (vanishingly rare, and a sign something is actually
        wrong — a retry loop, a metering bug — not routine daily exhaustion).
        """
        tier = classify(requested)
        if tier is None:
            return requested, None, "untracked", 0

        with self._lock:
            self._roll_day_locked()

            def fits(t: str) -> bool:
                # The reservation itself has to fit, not just the balance so
                # far; otherwise one big request slips through right at the
                # boundary and spends past the cap.
                return self._committed_locked(t) + estimate <= TIER_SPENDABLE[t]

            if tier == "openai_mini":
                if fits("openai_mini"):
                    self._reserved["openai_mini"] += estimate
                    return requested, "openai_mini", "as_is", estimate
                # No in-proxy downgrade target for mini — pr-agent's own
                # fallback_models list picks up from here (see module docstring).
                return requested, "openai_mini", "exhausted", 0

            # tier == "openai"
            if fits("openai"):
                self._reserved["openai"] += estimate
                return requested, "openai", "as_is", estimate
            if not DEEPSEEK_API_KEY:
                return DEEPSEEK_MODEL, "deepseek", "exhausted", 0
            if fits("deepseek"):
                self._reserved["deepseek"] += estimate
                return DEEPSEEK_MODEL, "deepseek", "deepseek", estimate
            return DEEPSEEK_MODEL, "deepseek", "exhausted", 0

    def settle(self, tier: str | None, reserved: int, tokens: int) -> None:
        """Release a reservation and charge what the call actually cost.

        A request admitted just before the daily rollover settles just after it
        and is charged to the new day. That is deliberate, not an oversight, and
        reviewers keep flagging it: usage is metered when a call completes, so
        the completion day is the day whose budget those tokens most likely came
        out of. Charging the admission day instead would leave today's counter
        understating what today's budget has really spent — and undercounting is
        the direction that ends in a bill (openai / openai_mini) or hides real
        spend (deepseek). The cost of this choice is bounded by one in-flight
        request per day, spent conservatively.
        """
        if not tier:
            return
        with self._lock:
            self._roll_day_locked()
            if reserved:
                # Day rollover zeroes `used` but not `reserved`; clamp regardless
                # so a double settle can never drive this negative.
                self._reserved[tier] = max(0, self._reserved[tier] - reserved)
            if tokens > 0:
                self._used[tier] += tokens
                self._save_locked()

    def snapshot(self) -> dict:
        with self._lock:
            out = {"day": self._day}
            for t in self.TIERS:
                out[f"{t}_used"] = self._used[t]
                out[f"{t}_reserved"] = self._reserved[t]
                out[f"{t}_spendable"] = TIER_SPENDABLE[t]
                out[f"{t}_budget"] = TIER_BUDGET[t]
            out["headroom"] = HEADROOM
            return out


STATE = QuotaState(STATE_PATH)


def _usage_line() -> str:
    snap = STATE.snapshot()

    def pct(used, cap):
        return f"{(100.0 * used / cap):.1f}%" if cap else "n/a"

    def tier(name):
        used, spendable = snap[f"{name}_used"], snap[f"{name}_spendable"]
        held = snap[f"{name}_reserved"]
        # In-flight reservations count against the budget, so show them or the
        # numbers look wrong mid-burst.
        extra = f" +{held:,} held" if held else ""
        return f"{name} {used:,}/{spendable:,} ({pct(used, spendable)}){extra}"

    return " | ".join(tier(t) for t in STATE.TIERS)


def extract_stream_tokens(raw: bytes) -> int:
    """Read a token count out of a buffered SSE (streaming) response body.

    Streamed replies only carry usage when the request asked for
    `stream_options: {"include_usage": true}`, and only in the final chunk.
    pr-agent streams for STREAMING_REQUIRED_MODELS only (currently just
    openai/qwq-plus) and can be made to stream by force_streaming_* settings, so
    nothing here streams today — but an unparsed stream would meter as zero,
    which is a silent metering hole in the direction that costs money.
    """
    total = 0
    for line in raw.split(b"\n"):
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except Exception:
            continue
        # Only the final chunk carries usage, and it covers the whole request.
        total = max(total, extract_total_tokens(chunk))
    return total


def extract_total_tokens(payload: dict) -> int:
    """Read a token count from a chat/completions (or Responses API) body.

    DeepSeek's API mirrors this same OpenAI-compatible `usage` shape, so no
    provider-specific handling is needed here.
    """
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return 0
    total = usage.get("total_tokens")
    if isinstance(total, int):
        return total
    # Responses API shape, and a defensive fallback if total_tokens is absent.
    prompt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    completion = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    try:
        return int(prompt) + int(completion)
    except (TypeError, ValueError):
        return 0


# ── HTTP proxy ────────────────────────────────────────────────────────────────

_HOP_BY_HOP = frozenset(
    ["connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
     "te", "trailers", "transfer-encoding", "upgrade"]
)
# Dropped so upstream replies in plain JSON and the body can be inspected
# without decompressing it.
_STRIPPED = _HOP_BY_HOP | {"host", "content-length", "accept-encoding"}

COMPLETION_PATHS = ("/chat/completions", "/completions", "/responses")


class _QuotaHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep litellm's connection pool happy

    def log_message(self, fmt, *args):  # suppress default Apache-style log
        pass

    def _reply(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _forward(
        self,
        method: str,
        body: bytes | None,
        base: str = OPENAI_BASE,
        auth_header: str | None = None,
    ) -> tuple[int, bytes, list[tuple[str, str]]]:
        url = base + self.path
        headers = {
            k: v for k, v in self.headers.items() if k.lower() not in _STRIPPED
        }
        if auth_header is not None:
            # Deepseek calls use this proxy's own key, not whatever the caller
            # sent — the caller only ever holds an OpenAI key.
            headers["Authorization"] = auth_header
        if body is not None:
            headers["Content-Length"] = str(len(body))
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
                return resp.status, resp.read(), list(resp.headers.items())
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), list(exc.headers.items())
        except (urllib.error.URLError, OSError) as exc:
            # DNS failure, refused connection or read timeout reaching upstream.
            # Answer with a real 503 rather than letting this escape the handler,
            # which would drop the connection and leave litellm with a transport
            # error instead of a status code.
            reason = getattr(exc, "reason", exc)
            print(f"[quota] ❌ upstream unreachable for {method} {self.path} ({base}): {reason}", flush=True)
            body_out = json.dumps(
                {
                    "error": {
                        "message": f"openai-quota-proxy: upstream unreachable ({reason})",
                        "type": "api_connection_error",
                        "code": "upstream_unreachable",
                    }
                }
            ).encode()
            return 503, body_out, [("Content-Type", "application/json")]

    def _relay(self, status: int, body: bytes, headers: list[tuple[str, str]]) -> None:
        self.send_response(status)
        for key, value in headers:
            if key.lower() in _HOP_BY_HOP or key.lower() == "content-length":
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") == "/__quota":
            self._reply(200, json.dumps(STATE.snapshot(), indent=2).encode())
            return
        status, body, headers = self._forward("GET", None)
        self._relay(status, body, headers)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0:
                raise ValueError("negative Content-Length")
        except ValueError:
            # Reply rather than raising, which would drop the connection with no
            # status line at all. The connection has to go regardless: framing is
            # unrecoverable, and any body bytes already sent would otherwise be
            # read as the start of the next request on a keep-alive connection.
            self.close_connection = True
            self._reply(
                400,
                json.dumps(
                    {
                        "error": {
                            "message": "openai-quota-proxy: invalid Content-Length",
                            "type": "invalid_request_error",
                        }
                    }
                ).encode(),
            )
            return
        body = self.rfile.read(length) if length > 0 else b""

        if not any(self.path.endswith(p) for p in COMPLETION_PATHS):
            status, resp_body, headers = self._forward("POST", body)
            self._relay(status, resp_body, headers)
            return

        try:
            payload = json.loads(body)
            requested = payload.get("model", "")
        except Exception:
            payload, requested = None, ""

        # Unparseable body or no model field: nothing to meter, pass it through.
        if payload is None or not requested:
            status, resp_body, headers = self._forward("POST", body)
            self._relay(status, resp_body, headers)
            return

        estimate = estimate_request_tokens(payload, len(body))
        model, tier, action, reserved = STATE.choose_model(requested, estimate)

        if action == "exhausted":
            if tier == "openai_mini":
                reason = "openai_mini free-tier budget is spent"
            elif not DEEPSEEK_API_KEY:
                reason = "DEEPSEEK__KEY is not set, cannot fall back to DeepSeek"
            else:
                reason = "deepseek circuit-breaker cap spent"
            print(f"[quota] 🛑 {reason} — refusing {requested}. {_usage_line()}", flush=True)
            error = {
                "error": {
                    "message": (
                        f"openai-quota-proxy: {reason} ({_usage_line()})."
                        " Refusing the request so it isn't billed or spent as"
                        " unbounded overage. Budgets reset at the next daily"
                        " rollover."
                    ),
                    "type": "rate_limit_error",
                    "code": "free_tier_daily_budget_exhausted",
                }
            }
            # 429 maps to litellm.RateLimitError, which pr-agent surfaces
            # without burning its retry budget — and, for openai_mini, lets
            # pr-agent's own fallback_models move on to the next entry.
            self._reply(429, json.dumps(error).encode())
            return

        target_base = OPENAI_BASE
        auth_header = None
        if action == "deepseek":
            print(f"[quota] ⬇️  openai spent — {requested} → {model} (DeepSeek). {_usage_line()}", flush=True)
            payload["model"] = model
            body = json.dumps(payload).encode()
            target_base = DEEPSEEK_BASE
            auth_header = f"Bearer {DEEPSEEK_API_KEY}"
        elif action == "untracked":
            print(f"[quota] ❔ {requested} is not a known free-tier model — passing through.", flush=True)

        tokens = 0
        try:
            status, resp_body, headers = self._forward(
                "POST", body, base=target_base, auth_header=auth_header
            )
            if status == 200:
                content_type = next(
                    (v for k, v in headers if k.lower() == "content-type"), ""
                )
                streamed = "text/event-stream" in content_type.lower()
                if streamed:
                    tokens = extract_stream_tokens(resp_body)
                else:
                    try:
                        tokens = extract_total_tokens(json.loads(resp_body))
                    except Exception:
                        tokens = 0

                if tokens:
                    print(f"[quota] {model} used {tokens:,} tokens", flush=True)
                elif tier:
                    # A successful call always spent tokens. If the response
                    # doesn't say how many, charge the pre-flight estimate rather
                    # than let it through free: otherwise a stream sent without
                    # stream_options.include_usage never moves the counters, and
                    # repeating it would walk straight past the daily caps.
                    # Over-charging only under-uses the budget; under-charging
                    # bills (openai / openai_mini) or hides real spend (deepseek).
                    tokens = estimate
                    why = "streamed with no usage chunk" if streamed else "reported no usage"
                    print(
                        f"[quota] ⚠️  {model} {why} — charging the {estimate:,}-token"
                        " estimate instead. Set stream_options.include_usage on"
                        " streaming requests so real usage can be metered.",
                        flush=True,
                    )
            else:
                # Errors aren't metered (neither provider bills them), but they
                # must be visible — this is how a block like an org spend limit
                # or an invalid DeepSeek key shows up.
                detail = resp_body[:300].decode(errors="replace")
                print(f"[quota] ⚠️  upstream HTTP {status} for {model}: {detail}", flush=True)
        finally:
            # Must run on every path, or a failed call leaks its reservation and
            # permanently shrinks the day's usable budget.
            STATE.settle(tier, reserved, tokens)

        print(f"[quota] {_usage_line()}", flush=True)
        self._relay(status, resp_body, headers)


if __name__ == "__main__":
    print(
        f"[quota] Listening on 127.0.0.1:{LISTEN_PORT} → {OPENAI_BASE}"
        f" (deepseek overflow → {DEEPSEEK_BASE}, model={DEEPSEEK_MODEL},"
        f" key {'set' if DEEPSEEK_API_KEY else 'MISSING'})\n"
        f"[quota] Budgets: "
        + " ".join(f"{t}={TIER_BUDGET[t]:,}" for t in STATE.TIERS)
        + f" (headroom {HEADROOM:.0%} → spendable "
        + "/".join(str(TIER_SPENDABLE[t]) for t in STATE.TIERS)
        + f")\n[quota] State file: {STATE_PATH}",
        flush=True,
    )
    http.server.ThreadingHTTPServer(("127.0.0.1", LISTEN_PORT), _QuotaHandler).serve_forever()
