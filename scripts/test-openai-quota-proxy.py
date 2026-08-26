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
        raise RuntimeError(f"proxy did not start: {proc.stdout.read() if proc.stdout else ''}")

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
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

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
