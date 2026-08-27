#!/usr/bin/env python3
"""
Quota-aware proxy that sits between pr-agent (litellm) and api.openai.com.

Why this exists
---------------
OpenAI's free daily grant has two buckets (see platform docs / the promo notice):

  tier 1  ~250K tokens/day   gpt-5.4, gpt-5.2, gpt-5.1, gpt-5, gpt-4.1, gpt-4o, o1, o3, ...
  tier 2  ~2.5M tokens/day   gpt-5.4-mini, gpt-5-mini, gpt-4.1-mini, o3-mini, ...

Usage past those limits is *billed*, not refused, so pr-agent's `fallback_models`
never triggers on quota exhaustion — the primary model keeps succeeding and
silently spends real credit. Conversely, an org-level enforced spend limit is
all-or-nothing: once tripped it blocks every model at once, so the fallback
can't help there either.

This proxy makes the switch proactive instead of error-driven: it counts the
tokens each response actually consumed, and once the tier-1 daily budget is
spent it rewrites the `model` field of subsequent requests to the tier-2
equivalent. When tier 2 is spent too it returns 429 rather than letting the
request through to be billed.

Wiring
------
pr-agent points at this proxy via `[openai] api_base` in pr-agent-override.toml.
The caller's Authorization header is forwarded untouched, so the proxy never
needs its own copy of the API key.

Environment variables
---------------------
  QUOTA_PROXY_PORT        Port to listen on                  (default 3002)
  OPENAI_UPSTREAM_BASE    Real API base                      (default https://api.openai.com)
  QUOTA_STATE_PATH        Where the daily counters live      (default /tmp/openai-quota-state.json)
  TIER1_DAILY_TOKENS      Tier-1 daily budget                (default 250000)
  TIER2_DAILY_TOKENS      Tier-2 daily budget                (default 2500000)
  QUOTA_HEADROOM          Fraction of budget actually usable (default 0.90)
  QUOTA_TZ_OFFSET_HOURS   Hour offset for the daily reset    (default 0 = UTC midnight)
  QUOTA_UPSTREAM_TIMEOUT  Upstream request timeout, seconds  (default 300)
  QUOTA_DEFAULT_TIER2     Downgrade target for unmapped tier-1 models

  QUOTA_PRESEED_DAY       Date (YYYY-MM-DD) the preseed below applies to
  QUOTA_PRESEED_TIER1     Tokens to treat as already spent on that day
  QUOTA_PRESEED_TIER2     Tokens to treat as already spent on that day

The preseed exists because the counters start at zero on a fresh deploy while
OpenAI's real grant may already be partly spent — sending tier-1 traffic then
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
STATE_PATH = os.environ.get("QUOTA_STATE_PATH", "/tmp/openai-quota-state.json")
UPSTREAM_TIMEOUT = float(os.environ.get("QUOTA_UPSTREAM_TIMEOUT", "300"))
TZ_OFFSET_HOURS = float(os.environ.get("QUOTA_TZ_OFFSET_HOURS", "0"))
DEFAULT_TIER2_MODEL = os.environ.get("QUOTA_DEFAULT_TIER2", "gpt-5.4-mini")

PRESEED_DAY = os.environ.get("QUOTA_PRESEED_DAY", "").strip()
PRESEED = {
    1: int(os.environ.get("QUOTA_PRESEED_TIER1", "0")),
    2: int(os.environ.get("QUOTA_PRESEED_TIER2", "0")),
}

# Only a fraction of each budget is spendable. Token cost is only known *after*
# a response comes back, so the remainder absorbs the request that crosses the
# line — without it a single large diff could overshoot into paid usage.
HEADROOM = float(os.environ.get("QUOTA_HEADROOM", "0.90"))

# Each in-flight request reserves an estimated cost against its tier until the
# real usage arrives. Without a reservation, concurrent requests would all read
# the same remaining balance, pass the check together, and collectively overshoot
# the cap by far more than HEADROOM absorbs.
RESERVE_MIN = int(os.environ.get("QUOTA_RESERVE_MIN", "2000"))
RESERVE_COMPLETION_DEFAULT = int(os.environ.get("QUOTA_RESERVE_COMPLETION", "4000"))

TIER_BUDGET = {
    1: int(os.environ.get("TIER1_DAILY_TOKENS", "250000")),
    2: int(os.environ.get("TIER2_DAILY_TOKENS", "2500000")),
}
TIER_SPENDABLE = {tier: int(budget * HEADROOM) for tier, budget in TIER_BUDGET.items()}

TIER1_MODELS = frozenset(
    [
        "gpt-5.4", "gpt-5.2", "gpt-5.1", "gpt-5.1-codex", "gpt-5",
        "gpt-5-codex", "gpt-5-chat-latest", "gpt-4.1", "gpt-4o", "o1", "o3",
    ]
)
TIER2_MODELS = frozenset(
    [
        "gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.1-codex-mini", "gpt-5-mini",
        "gpt-5-nano", "gpt-4.1-mini", "gpt-4.1-nano", "gpt-4o-mini",
        "o1-mini", "o3-mini", "o4-mini", "codex-mini-latest",
    ]
)

# Tier-1 model -> its closest tier-2 sibling.
DOWNGRADE = {
    "gpt-5.4": "gpt-5.4-mini",
    "gpt-5.2": "gpt-5.4-mini",
    "gpt-5.1": "gpt-5-mini",
    "gpt-5": "gpt-5-mini",
    "gpt-5-chat-latest": "gpt-5-mini",
    "gpt-5-codex": "gpt-5.1-codex-mini",
    "gpt-5.1-codex": "gpt-5.1-codex-mini",
    "gpt-4.1": "gpt-4.1-mini",
    "gpt-4o": "gpt-4o-mini",
    "o1": "o1-mini",
    "o3": "o3-mini",
}

_DATE_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")


def normalize_model(name: str) -> str:
    """'openai/gpt-5.4-2026-03-05' -> 'gpt-5.4' so dated snapshots classify."""
    base = name.strip().lower().rsplit("/", 1)[-1]
    return _DATE_SUFFIX.sub("", base)


def classify(name: str) -> int | None:
    """Return 1, 2, or None when the model isn't part of a free-tier bucket."""
    norm = normalize_model(name)
    if norm in TIER2_MODELS:  # checked first: 'gpt-5.4-mini' also prefixes 'gpt-5.4'
        return 2
    if norm in TIER1_MODELS:
        return 1
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

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._day = current_day()
        self._used = {1: 0, 2: 0}
        self._reserved = {1: 0, 2: 0}
        self._load()

    def _committed_locked(self, tier: int) -> int:
        return self._used[tier] + self._reserved[tier]

    def _apply_preseed(self) -> None:
        """Treat part of today's grant as already spent (see QUOTA_PRESEED_DAY)."""
        if not PRESEED_DAY or PRESEED_DAY != self._day:
            return
        if not (PRESEED[1] or PRESEED[2]):
            return
        self._used = {1: PRESEED[1], 2: PRESEED[2]}
        print(
            f"[quota] Preseeded {self._day} as already spent:"
            f" tier1={self._used[1]:,} tier2={self._used[2]:,}",
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
        self._used = {1: int(data.get("tier1", 0)), 2: int(data.get("tier2", 0))}
        print(
            f"[quota] Resumed {self._day}: tier1={self._used[1]:,} tier2={self._used[2]:,}",
            flush=True,
        )

    def _save_locked(self) -> None:
        payload = {"day": self._day, "tier1": self._used[1], "tier2": self._used[2]}
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
            print(
                f"[quota] 🔄 New day {today} — resetting"
                f" (spent {self._used[1]:,} tier1 / {self._used[2]:,} tier2 on {self._day})",
                flush=True,
            )
            self._day = today
            self._used = {1: 0, 2: 0}
            self._save_locked()

    def choose_model(self, requested: str, estimate: int) -> tuple[str, int | None, str, int]:
        """Pick the model to actually call and reserve its estimated cost.

        Returns (model, tier_to_charge, action, reserved) where action is one of
        'as_is', 'downgraded', 'exhausted', 'untracked'. Every non-zero
        `reserved` must be handed back to settle() exactly once.
        """
        tier = classify(requested)
        if tier is None:
            return requested, None, "untracked", 0

        with self._lock:
            self._roll_day_locked()

            def fits(t: int) -> bool:
                # The reservation itself has to fit, not just the balance so far;
                # otherwise one big request slips through right at the boundary
                # and spends past the cap.
                return self._committed_locked(t) + estimate <= TIER_SPENDABLE[t]

            if tier == 1:
                if fits(1):
                    self._reserved[1] += estimate
                    return requested, 1, "as_is", estimate
                target = DOWNGRADE.get(normalize_model(requested), DEFAULT_TIER2_MODEL)
                if fits(2):
                    self._reserved[2] += estimate
                    return target, 2, "downgraded", estimate
                return target, 2, "exhausted", 0

            if fits(2):
                self._reserved[2] += estimate
                return requested, 2, "as_is", estimate
            return requested, 2, "exhausted", 0

    def settle(self, tier: int, reserved: int, tokens: int) -> None:
        """Release a reservation and charge what the call actually cost.

        A request admitted just before the daily rollover settles just after it
        and is charged to the new day. That is deliberate, not an oversight, and
        reviewers keep flagging it: OpenAI meters a call when it completes, so
        the completion day is the day whose grant those tokens most likely came
        out of. Charging the admission day instead would leave today's counter
        understating what today's grant has really spent — and undercounting is
        the direction that ends in a bill. The cost of this choice is bounded by
        one in-flight request per day, spent conservatively.
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
            return {
                "day": self._day,
                "tier1_used": self._used[1],
                "tier2_used": self._used[2],
                "tier1_reserved": self._reserved[1],
                "tier2_reserved": self._reserved[2],
                "tier1_spendable": TIER_SPENDABLE[1],
                "tier2_spendable": TIER_SPENDABLE[2],
                "tier1_budget": TIER_BUDGET[1],
                "tier2_budget": TIER_BUDGET[2],
                "headroom": HEADROOM,
            }


STATE = QuotaState(STATE_PATH)


def _usage_line() -> str:
    snap = STATE.snapshot()

    def pct(used, cap):
        return f"{(100.0 * used / cap):.1f}%" if cap else "n/a"

    def tier(n):
        used, spendable = snap[f"tier{n}_used"], snap[f"tier{n}_spendable"]
        held = snap[f"tier{n}_reserved"]
        # In-flight reservations count against the budget, so show them or the
        # numbers look wrong mid-burst.
        extra = f" +{held:,} held" if held else ""
        return f"tier{n} {used:,}/{spendable:,} ({pct(used, spendable)}){extra}"

    return f"{tier(1)} | {tier(2)}"


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
    """Read a token count from a chat/completions (or Responses API) body."""
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

    def _forward(self, method: str, body: bytes | None) -> tuple[int, bytes, list[tuple[str, str]]]:
        url = OPENAI_BASE + self.path
        headers = {
            k: v for k, v in self.headers.items() if k.lower() not in _STRIPPED
        }
        if body is not None:
            headers["Content-Length"] = str(len(body))
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
                return resp.status, resp.read(), list(resp.headers.items())
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), list(exc.headers.items())
        except (urllib.error.URLError, OSError) as exc:
            # DNS failure, refused connection or read timeout reaching OpenAI.
            # Answer with a real 503 rather than letting this escape the handler,
            # which would drop the connection and leave litellm with a transport
            # error instead of a status code.
            reason = getattr(exc, "reason", exc)
            print(f"[quota] ❌ upstream unreachable for {method} {self.path}: {reason}", flush=True)
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
            print(
                f"[quota] 🛑 Both free tiers spent — refusing {requested}. {_usage_line()}",
                flush=True,
            )
            error = {
                "error": {
                    "message": (
                        "openai-quota-proxy: daily free-tier token budget is spent"
                        f" ({_usage_line()}). Refusing the request so it isn't billed"
                        " as paid usage. Budgets reset at the next daily rollover."
                    ),
                    "type": "rate_limit_error",
                    "code": "free_tier_daily_budget_exhausted",
                }
            }
            # 429 maps to litellm.RateLimitError, which pr-agent surfaces without
            # burning its retry budget.
            self._reply(429, json.dumps(error).encode())
            return

        if action == "downgraded":
            print(f"[quota] ⬇️  tier1 spent — {requested} → {model}. {_usage_line()}", flush=True)
            payload["model"] = model
            body = json.dumps(payload).encode()
        elif action == "untracked":
            print(f"[quota] ❔ {requested} is not a known free-tier model — passing through.", flush=True)

        tokens = 0
        try:
            status, resp_body, headers = self._forward("POST", body)
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
                    # repeating it would walk straight past both daily caps.
                    # Over-charging only under-uses the grant; under-charging bills.
                    tokens = estimate
                    why = "streamed with no usage chunk" if streamed else "reported no usage"
                    print(
                        f"[quota] ⚠️  {model} {why} — charging the {estimate:,}-token"
                        " estimate instead. Set stream_options.include_usage on"
                        " streaming requests so real usage can be metered.",
                        flush=True,
                    )
            else:
                # Errors aren't metered (OpenAI doesn't bill them), but they must be
                # visible — this is how a block like an org spend limit shows up.
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
        f"[quota] Listening on 127.0.0.1:{LISTEN_PORT} → {OPENAI_BASE}\n"
        f"[quota] Budgets: tier1 {TIER_BUDGET[1]:,} tier2 {TIER_BUDGET[2]:,}"
        f" (headroom {HEADROOM:.0%} → spendable {TIER_SPENDABLE[1]:,}/{TIER_SPENDABLE[2]:,})\n"
        f"[quota] State file: {STATE_PATH}",
        flush=True,
    )
    http.server.ThreadingHTTPServer(("127.0.0.1", LISTEN_PORT), _QuotaHandler).serve_forever()
