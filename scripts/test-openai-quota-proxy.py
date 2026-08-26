#!/usr/bin/env python3
"""
Tests fly/openai-quota-proxy.py against a fake OpenAI upstream.

Run with:  python scripts/test-openai-quota-proxy.py

No dependencies and no network access — a stub upstream stands in for
api.openai.com, so this is safe to run anywhere. Exits non-zero on failure.
"""

import http.server
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROXY_SRC = os.path.join(REPO_ROOT, "fly", "openai-quota-proxy.py")

TOKENS_PER_CALL = 400
TIER1_BUDGET = 1000
TIER2_BUDGET = 2000

# Mutated by individual cases to slow the upstream down or force a failure.
RESPONSE_DELAY = 0.0
FORCE_STATUS: int | None = None
# "usage" -> SSE ending in a usage chunk; "nousage" -> SSE without one.
FORCE_STREAM: str | None = None

seen_models: list[str] = []
seen_auth: list[str] = []
failures: list[str] = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r} want {want!r}")
    if not ok:
        failures.append(label)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeOpenAI(http.server.BaseHTTPRequestHandler):
    """Echoes back the model it was asked for, with a fixed token cost."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n))
        seen_models.append(req.get("model"))
        seen_auth.append(self.headers.get("Authorization"))
        if RESPONSE_DELAY:
            time.sleep(RESPONSE_DELAY)
        if FORCE_STATUS:
            err = json.dumps({"error": {"message": "forced failure"}}).encode()
            self.send_response(FORCE_STATUS)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)
            return
        if FORCE_STREAM:
            chunks = [
                {"choices": [{"delta": {"content": "ok"}, "index": 0}], "usage": None},
            ]
            if FORCE_STREAM == "usage":
                chunks.append(
                    {"choices": [], "usage": {"total_tokens": TOKENS_PER_CALL}}
                )
            sse = b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks)
            sse += b"data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(sse)))
            self.end_headers()
            self.wfile.write(sse)
            return
        body = json.dumps(
            {
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "model": req.get("model"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": TOKENS_PER_CALL - 100,
                    "completion_tokens": 100,
                    "total_tokens": TOKENS_PER_CALL,
                },
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="quota-proxy-test-")
    state = os.path.join(tmpdir, "state.json")
    upstream_port = free_port()
    proxy_port = free_port()

    def rm_state():
        if os.path.exists(state):
            os.remove(state)

    def start_proxy(extra_env=None):
        env = {
            **os.environ,
            "QUOTA_PROXY_PORT": str(proxy_port),
            "OPENAI_UPSTREAM_BASE": f"http://127.0.0.1:{upstream_port}",
            "QUOTA_STATE_PATH": state,
            "TIER1_DAILY_TOKENS": str(TIER1_BUDGET),
            "TIER2_DAILY_TOKENS": str(TIER2_BUDGET),
            "QUOTA_HEADROOM": "1.0",
            # Small reservations so the tiny budgets here behave predictably; the
            # production defaults would exceed a 1000-token test budget outright.
            "QUOTA_RESERVE_MIN": "0",
            "QUOTA_RESERVE_COMPLETION": "100",
            **(extra_env or {}),
        }
        # Drop inherited preseed vars so each case controls its own.
        for key in ("QUOTA_PRESEED_DAY", "QUOTA_PRESEED_TIER1", "QUOTA_PRESEED_TIER2"):
            if extra_env is None or key not in extra_env:
                env.pop(key, None)
        proc = subprocess.Popen(
            [sys.executable, PROXY_SRC],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for _ in range(80):
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{proxy_port}/__quota", timeout=1
                ).read()
                return proc
            except Exception:
                time.sleep(0.1)
        # Terminate before reading: if the process is alive but never became
        # ready, an unbounded read() on its still-open stdout would hang the
        # suite instead of failing it.
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
        raise RuntimeError(f"proxy did not start: {out}")

    def call(model="gpt-5.4-2026-03-05"):
        req = urllib.request.Request(
            f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
            data=json.dumps(
                {"model": model, "messages": [{"role": "user", "content": "hi"}]}
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer sk-test-123",
            },
            method="POST",
        )
        def parse(raw: bytes):
            # SSE bodies aren't JSON; hand those back as text.
            try:
                return json.loads(raw)
            except ValueError:
                return raw.decode(errors="replace")

        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, parse(r.read())
        except urllib.error.HTTPError as e:
            return e.code, parse(e.read())

    def quota():
        with urllib.request.urlopen(f"http://127.0.0.1:{proxy_port}/__quota", timeout=5) as r:
            return json.loads(r.read())

    # ── model classification (imported directly, no server needed) ────────────
    print("\n=== classify / normalize ===")
    spec = importlib.util.spec_from_file_location("qp", PROXY_SRC)
    qp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(qp)

    check("normalize dated snapshot", qp.normalize_model("gpt-5.4-2026-03-05"), "gpt-5.4")
    check("normalize mini snapshot", qp.normalize_model("gpt-5.4-mini-2026-03-17"), "gpt-5.4-mini")
    check("normalize provider prefix", qp.normalize_model("openai/gpt-4.1-2025-04-14"), "gpt-4.1")
    check("classify gpt-5.4 dated", qp.classify("gpt-5.4-2026-03-05"), 1)
    check("classify gpt-5.4-mini", qp.classify("gpt-5.4-mini"), 2)
    check("classify gpt-4.1-mini", qp.classify("gpt-4.1-mini"), 2)
    check("classify o3", qp.classify("o3"), 1)
    check("classify o3-mini", qp.classify("o3-mini"), 2)
    check("classify unknown", qp.classify("claude-opus-5"), None)
    check("usage total_tokens", qp.extract_total_tokens({"usage": {"total_tokens": 42}}), 42)
    check(
        "usage responses shape",
        qp.extract_total_tokens({"usage": {"input_tokens": 10, "output_tokens": 5}}),
        15,
    )
    check("usage missing", qp.extract_total_tokens({}), 0)

    # ── end to end ────────────────────────────────────────────────────────────
    rm_state()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", upstream_port), FakeOpenAI)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    proc = start_proxy()
    out = ""
    try:
        print("\n=== tier1 spend, then downgrade ===")
        for i in range(3):  # 3 * 400 = 1200, crosses the 1000 budget
            status, _ = call()
            check(f"call {i+1} status", status, 200)
        check("tier1 used after 3 calls", quota()["tier1_used"], 1200)
        check("model untouched while tier1 has budget", seen_models[:3], ["gpt-5.4-2026-03-05"] * 3)
        check("auth header forwarded verbatim", seen_auth[0], "Bearer sk-test-123")

        status, resp = call()
        check("downgraded call status", status, 200)
        check("upstream got the mini model", seen_models[3], "gpt-5.4-mini")
        check("response echoes mini", resp["model"], "gpt-5.4-mini")
        check("tier1 not charged for a tier2 call", quota()["tier1_used"], 1200)
        check("tier2 charged instead", quota()["tier2_used"], 400)

        print("\n=== tier2 spend, then hard stop ===")
        for i in range(4):  # 400 + 4*400 = 2000, reaching the tier2 budget
            status, _ = call()
            check(f"tier2 call {i+1} status", status, 200)
        check("tier2 used", quota()["tier2_used"], 2000)

        status, resp = call()
        check("exhausted returns 429", status, 429)
        check("exhausted error code", resp["error"]["code"], "free_tier_daily_budget_exhausted")
        check("exhausted request never reached upstream", len(seen_models), 8)

        print("\n=== explicit tier2 request while tier2 exhausted ===")
        status, _ = call(model="gpt-4.1-mini")
        check("explicit tier2 refused", status, 429)

        print("\n=== unknown model passes through untracked ===")
        before = len(seen_models)
        status, _ = call(model="some-custom-model")
        check("untracked forwarded", status, 200)
        check("untracked reached upstream", len(seen_models), before + 1)
        check("untracked not metered", quota()["tier2_used"], 2000)

        print("\n=== counters survive a restart ===")
        proc.terminate(); proc.wait(timeout=10)
        proc = start_proxy()
        check("tier1 resumed", quota()["tier1_used"], 1200)
        check("tier2 resumed", quota()["tier2_used"], 2000)

        print("\n=== daily rollover resets counters ===")
        proc.terminate(); proc.wait(timeout=10)
        with open(state) as fh:
            saved = json.load(fh)
        saved["day"] = "2020-01-01"
        with open(state, "w") as fh:
            json.dump(saved, fh)
        proc = start_proxy()
        check("tier1 reset", quota()["tier1_used"], 0)
        check("tier2 reset", quota()["tier2_used"], 0)
        call()
        check("primary model in use again", seen_models[-1], "gpt-5.4-2026-03-05")

        print("\n=== headroom math ===")
        proc.terminate(); proc.wait(timeout=10)
        rm_state()
        proc = start_proxy(
            {
                "TIER1_DAILY_TOKENS": "250000",
                "TIER2_DAILY_TOKENS": "2500000",
                "QUOTA_HEADROOM": "0.90",
            }
        )
        snap = quota()
        check("tier1 spendable is 90% of 250k", snap["tier1_spendable"], 225000)
        check("tier2 spendable is 90% of 2.5M", snap["tier2_spendable"], 2250000)

        print("\n=== preseed marks today's grant as already spent ===")
        proc.terminate(); proc.wait(timeout=10)
        rm_state()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        proc = start_proxy({"QUOTA_PRESEED_DAY": today, "QUOTA_PRESEED_TIER1": str(TIER1_BUDGET)})
        check("preseed applied to tier1", quota()["tier1_used"], TIER1_BUDGET)
        check("preseed left tier2 alone", quota()["tier2_used"], 0)
        before = len(seen_models)
        call()
        check("first call already downgraded", seen_models[before], "gpt-5.4-mini")

        print("\n=== preseed for another day is ignored ===")
        proc.terminate(); proc.wait(timeout=10)
        rm_state()
        proc = start_proxy({"QUOTA_PRESEED_DAY": "2020-01-01", "QUOTA_PRESEED_TIER1": "1000"})
        check("stale preseed not applied", quota()["tier1_used"], 0)

        print("\n=== saved state beats preseed ===")
        call()
        check("call recorded", quota()["tier1_used"], 400)
        proc.terminate(); proc.wait(timeout=10)
        proc = start_proxy({"QUOTA_PRESEED_DAY": today, "QUOTA_PRESEED_TIER1": "999"})
        check("saved state wins", quota()["tier1_used"], 400)

        # A request reserves its estimated cost while in flight, so parallel
        # requests can't each be told the same budget is free. Before that, the
        # budget was only charged after a response arrived, so all 12 below would
        # read used=0 and be admitted to tier 1 — the check was simply inert
        # under concurrency, and a burst of costlier calls could overshoot the
        # cap into billable usage. This is the race pr-agent flagged on PR #2.
        print("\n=== concurrent requests cannot collectively overshoot ===")
        global RESPONSE_DELAY
        proc.terminate(); proc.wait(timeout=10)
        rm_state()
        seen_models.clear()
        RESPONSE_DELAY = 1.0  # hold every call open so all 12 overlap
        # Pin the reservation to exactly 2500 so admissions are arithmetic:
        # admit while committed + 2500 <= 10000, i.e. at 0/2500/5000/7500.
        proc = start_proxy(
            {
                "TIER1_DAILY_TOKENS": "10000",
                "TIER2_DAILY_TOKENS": "1000000",
                "QUOTA_HEADROOM": "1.0",
                "QUOTA_RESERVE_MIN": "2500",
                "QUOTA_RESERVE_COMPLETION": "0",
            }
        )
        results: list[int] = []
        threads = [
            threading.Thread(target=lambda: results.append(call()[0])) for _ in range(12)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        RESPONSE_DELAY = 0.0

        snap = quota()
        tier1_calls = sum(1 for m in seen_models if m == "gpt-5.4-2026-03-05")
        tier2_calls = sum(1 for m in seen_models if m == "gpt-5.4-mini")
        check("all 12 requests answered", len(results), 12)
        check("none rejected", [r for r in results if r != 200], [])
        check("only 4 admitted to tier1", tier1_calls, 4)
        check("the rest downgraded to tier2", tier2_calls, 8)
        check("tier1 reservations never exceeded budget", snap["tier1_used"] <= 10000, True)
        check("tier1 used exactly 4 calls' worth", snap["tier1_used"], 4 * TOKENS_PER_CALL)
        check("tier2 charged the other 8", snap["tier2_used"], 8 * TOKENS_PER_CALL)
        check("tier1 reservations all released", snap["tier1_reserved"], 0)
        check("tier2 reservations all released", snap["tier2_reserved"], 0)

        print("\n=== a reservation too big for the tier is not admitted ===")
        proc.terminate(); proc.wait(timeout=10)
        rm_state()
        seen_models.clear()
        # One request whose estimate alone exceeds the tier-1 budget must not be
        # sent on tier 1 just because the balance is still zero.
        proc = start_proxy(
            {
                "TIER1_DAILY_TOKENS": "5000",
                "TIER2_DAILY_TOKENS": "1000000",
                "QUOTA_HEADROOM": "1.0",
                "QUOTA_RESERVE_MIN": "9000",
                "QUOTA_RESERVE_COMPLETION": "0",
            }
        )
        status, _ = call()
        check("oversized request downgraded, not admitted", seen_models[-1], "gpt-5.4-mini")
        check("tier1 untouched", quota()["tier1_used"], 0)

        print("\n=== failed upstream call releases its reservation ===")
        global FORCE_STATUS
        proc.terminate(); proc.wait(timeout=10)
        rm_state()
        proc = start_proxy()
        FORCE_STATUS = 500
        status, _ = call()
        FORCE_STATUS = None
        snap = quota()
        check("error relayed to caller", status, 500)
        check("nothing metered for a failed call", snap["tier1_used"], 0)
        check("reservation released, not leaked", snap["tier1_reserved"], 0)
        # Budget must be fully usable again after the failure.
        status, _ = call()
        check("next call still admitted to tier1", seen_models[-1], "gpt-5.4-2026-03-05")

        print("\n=== streamed response is still metered ===")
        global FORCE_STREAM
        proc.terminate(); proc.wait(timeout=10)
        rm_state()
        proc = start_proxy()
        FORCE_STREAM = "usage"
        status, _ = call()
        FORCE_STREAM = None
        snap = quota()
        check("stream relayed", status, 200)
        check("usage chunk metered", snap["tier1_used"], TOKENS_PER_CALL)
        check("no reservation left held", snap["tier1_reserved"], 0)

        print("\n=== stream with no usage chunk: unmetered but released ===")
        proc.terminate(); proc.wait(timeout=10)
        rm_state()
        proc = start_proxy()
        FORCE_STREAM = "nousage"
        status, _ = call()
        FORCE_STREAM = None
        snap = quota()
        check("stream relayed", status, 200)
        check("nothing metered (cannot see usage)", snap["tier1_used"], 0)
        check("reservation still released", snap["tier1_reserved"], 0)

        print("\n=== malformed Content-Length answers 400, not a dropped socket ===")
        req = urllib.request.Request(
            f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
            data=b'{"model": "gpt-5.4-2026-03-05"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        req.add_header("Content-Length", "not-a-number")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                bad_status = r.status
        except urllib.error.HTTPError as e:
            bad_status = e.code
        except Exception as exc:  # a dropped connection would land here
            bad_status = f"raised {type(exc).__name__}"
        check("malformed header gets a status code", bad_status, 400)

        print("\n=== unreachable upstream answers 503, not a dropped connection ===")
        proc.terminate(); proc.wait(timeout=10)
        rm_state()
        dead_port = free_port()  # nothing is listening here
        proc = start_proxy({"OPENAI_UPSTREAM_BASE": f"http://127.0.0.1:{dead_port}"})
        status, resp = call()
        check("got a real status code", status, 503)
        check("error code identifies the cause", resp["error"]["code"], "upstream_unreachable")
        snap = quota()
        check("nothing metered", snap["tier1_used"], 0)
        check("reservation released", snap["tier1_reserved"], 0)
    finally:
        proc.terminate()
        try:
            out = proc.stdout.read() if proc.stdout else ""
        except Exception:
            out = ""
        srv.shutdown()

    if failures:
        print(f"\n--- proxy log (last run) ---\n{out[-1500:]}")
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
