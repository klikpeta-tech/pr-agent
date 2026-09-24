#!/usr/bin/env python3
"""
Tests fly/openai-quota-proxy.py against fake OpenAI and DeepSeek upstreams.

Run with:  python scripts/test-openai-quota-proxy.py

No dependencies and no network access — stub upstreams stand in for
api.openai.com and api.deepseek.com, so this is safe to run anywhere. Exits
non-zero on failure.
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
OPENAI_BUDGET = 1000
OPENAI_MINI_BUDGET = 2000
DEEPSEEK_BUDGET = 2000
FAKE_DEEPSEEK_KEY = "sk-deepseek-test-key"

# Mutated by individual cases to slow an upstream down or force a failure.
RESPONSE_DELAY = 0.0
FORCE_STATUS: int | None = None
# "usage" -> SSE ending in a usage chunk; "nousage" -> SSE without one.
FORCE_STREAM: str | None = None
# Return a normal 200 JSON body with the "usage" object omitted entirely.
FORCE_NO_USAGE = False

seen_models: list[str] = []
seen_auth: list[str] = []
seen_deepseek_models: list[str] = []
seen_deepseek_auth: list[str] = []
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


def _make_fake_handler(models: list, auths: list):
    """Build a fake OpenAI-compatible upstream handler that echoes back the
    model it was asked for, with a fixed token cost, recording into the given
    lists so OpenAI and DeepSeek traffic can be told apart."""

    class FakeUpstream(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n))
            models.append(req.get("model"))
            auths.append(self.headers.get("Authorization"))
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
            payload = {
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
            if FORCE_NO_USAGE:
                payload.pop("usage")
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return FakeUpstream


FakeOpenAI = _make_fake_handler(seen_models, seen_auth)
FakeDeepSeek = _make_fake_handler(seen_deepseek_models, seen_deepseek_auth)


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="quota-proxy-test-")
    state = os.path.join(tmpdir, "state.json")
    openai_port = free_port()
    deepseek_port = free_port()
    proxy_port = free_port()

    def rm_state():
        if os.path.exists(state):
            os.remove(state)

    def start_proxy(extra_env=None, deepseek_key=FAKE_DEEPSEEK_KEY):
        env = {
            **os.environ,
            "QUOTA_PROXY_PORT": str(proxy_port),
            "OPENAI_UPSTREAM_BASE": f"http://127.0.0.1:{openai_port}",
            "DEEPSEEK_UPSTREAM_BASE": f"http://127.0.0.1:{deepseek_port}",
            "DEEPSEEK__KEY": deepseek_key or "",
            "QUOTA_STATE_PATH": state,
            "OPENAI_DAILY_TOKENS": str(OPENAI_BUDGET),
            "OPENAI_MINI_DAILY_TOKENS": str(OPENAI_MINI_BUDGET),
            "DEEPSEEK_DAILY_TOKENS": str(DEEPSEEK_BUDGET),
            "QUOTA_HEADROOM": "1.0",
            # Small reservations so the tiny budgets here behave predictably; the
            # production defaults would exceed a 1000-token test budget outright.
            "QUOTA_RESERVE_MIN": "0",
            "QUOTA_RESERVE_COMPLETION": "100",
            **(extra_env or {}),
        }
        # Drop inherited preseed vars so each case controls its own.
        for key in ("QUOTA_PRESEED_DAY", "QUOTA_PRESEED_OPENAI", "QUOTA_PRESEED_OPENAI_MINI"):
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
    check("classify gpt-5.4 dated", qp.classify("gpt-5.4-2026-03-05"), "openai")
    check("classify gpt-5.4-mini", qp.classify("gpt-5.4-mini"), "openai_mini")
    check("classify gpt-4.1-mini", qp.classify("gpt-4.1-mini"), "openai_mini")
    check("classify o3", qp.classify("o3"), "openai")
    check("classify o3-mini", qp.classify("o3-mini"), "openai_mini")
    check("classify unknown", qp.classify("claude-opus-5"), None)
    check("classify deepseek is never a direct request", qp.classify("deepseek-v4-flash"), None)
    check("usage total_tokens", qp.extract_total_tokens({"usage": {"total_tokens": 42}}), 42)
    check(
        "usage responses shape",
        qp.extract_total_tokens({"usage": {"input_tokens": 10, "output_tokens": 5}}),
        15,
    )
    check("usage missing", qp.extract_total_tokens({}), 0)

    # ── end to end ────────────────────────────────────────────────────────────
    rm_state()
    openai_srv = http.server.ThreadingHTTPServer(("127.0.0.1", openai_port), FakeOpenAI)
    threading.Thread(target=openai_srv.serve_forever, daemon=True).start()
    deepseek_srv = http.server.ThreadingHTTPServer(("127.0.0.1", deepseek_port), FakeDeepSeek)
    threading.Thread(target=deepseek_srv.serve_forever, daemon=True).start()
    proc = start_proxy()
    out = ""
    try:
        print("\n=== openai spend, then overflow to deepseek ===")
        for i in range(3):  # 3 * 400 = 1200, crosses the 1000 budget
            status, _ = call()
            check(f"call {i+1} status", status, 200)
        check("openai used after 3 calls", quota()["openai_used"], 1200)
        check("model untouched while openai has budget", seen_models[:3], ["gpt-5.4-2026-03-05"] * 3)
        check("auth header forwarded verbatim to openai", seen_auth[0], "Bearer sk-test-123")

        status, resp = call()
        check("overflow call status", status, 200)
        check("openai upstream not hit for the overflow call", len(seen_models), 3)
        check("deepseek upstream got the deepseek model", seen_deepseek_models[0], "deepseek-v4-flash")
        check("response echoes deepseek", resp["model"], "deepseek-v4-flash")
        # The proxy injects its own DeepSeek key rather than forwarding the
        # caller's OpenAI bearer token — the caller couldn't authenticate to
        # DeepSeek with it even if it tried.
        check(
            "deepseek call uses the proxy's own key, not the caller's",
            seen_deepseek_auth[0],
            f"Bearer {FAKE_DEEPSEEK_KEY}",
        )
        check("openai not charged for the overflow call", quota()["openai_used"], 1200)
        check("deepseek charged instead", quota()["deepseek_used"], 400)

        print("\n=== deepseek circuit-breaker spent, then hard stop ===")
        for i in range(4):  # 400 + 4*400 = 2000, reaching the deepseek cap
            status, _ = call()
            check(f"deepseek call {i+1} status", status, 200)
        check("deepseek used", quota()["deepseek_used"], 2000)

        status, resp = call()
        check("exhausted returns 429", status, 429)
        check("exhausted error code", resp["error"]["code"], "free_tier_daily_budget_exhausted")
        check("exhausted request never reached either upstream", len(seen_models) + len(seen_deepseek_models), 3 + 5)

        print("\n=== openai_mini is tracked separately, with no in-proxy downgrade ===")
        proc.terminate(); proc.wait(timeout=10)
        rm_state()
        seen_models.clear(); seen_deepseek_models.clear()
        proc = start_proxy()
        for i in range(5):  # 5 * 400 = 2000, reaching the 2000 mini budget
            status, resp = call(model="gpt-4.1-mini")
            check(f"mini call {i+1} status", status, 200)
            check(f"mini call {i+1} hit openai, not deepseek", resp["model"], "gpt-4.1-mini")
        check("openai_mini used", quota()["openai_mini_used"], 2000)
        check("openai untouched by mini calls", quota()["openai_used"], 0)

        status, resp = call(model="gpt-4.1-mini")
        check("mini exhausted refused, not downgraded", status, 429)
        check("mini exhaustion never reaches deepseek", seen_deepseek_models, [])

        print("\n=== DEEPSEEK__KEY missing means openai exhaustion hard-stops too ===")
        proc.terminate(); proc.wait(timeout=10)
        rm_state()
        seen_models.clear(); seen_deepseek_models.clear()
        proc = start_proxy(deepseek_key="")
        for _ in range(3):
            call()
        status, resp = call()
        check("refused without a key", status, 429)
        check("reason mentions the missing key", "DEEPSEEK__KEY" in resp["error"]["message"], True)
        check("never reached deepseek", seen_deepseek_models, [])

        print("\n=== unknown model passes through untracked ===")
        proc.terminate(); proc.wait(timeout=10)
        rm_state()
        seen_models.clear(); seen_deepseek_models.clear()
        proc = start_proxy()
        before = len(seen_models)
        status, _ = call(model="some-custom-model")
        check("untracked forwarded", status, 200)
        check("untracked reached openai upstream", len(seen_models), before + 1)
        check("untracked not metered", quota()["openai_used"], 0)

        print("\n=== counters survive a restart ===")
        call()  # openai: 400
        call(model="gpt-4.1-mini")  # openai_mini: 400
        proc.terminate(); proc.wait(timeout=10)
        proc = start_proxy()
        check("openai resumed", quota()["openai_used"], 400)
        check("openai_mini resumed", quota()["openai_mini_used"], 400)

        print("\n=== daily rollover resets counters ===")
        proc.terminate(); proc.wait(timeout=10)
        with open(state) as fh:
            saved = json.load(fh)
        saved["day"] = "2020-01-01"
        with open(state, "w") as fh:
            json.dump(saved, fh)
        proc = start_proxy()
        check("openai reset", quota()["openai_used"], 0)
        check("openai_mini reset", quota()["openai_mini_used"], 0)
        seen_models.clear()
        call()
        check("primary model in use again", seen_models[-1], "gpt-5.4-2026-03-05")

        print("\n=== headroom math ===")
        proc.terminate(); proc.wait(timeout=10)
        rm_state()
        proc = start_proxy(
            {
                "OPENAI_DAILY_TOKENS": "250000",
                "OPENAI_MINI_DAILY_TOKENS": "2500000",
                "DEEPSEEK_DAILY_TOKENS": "20000000",
                "QUOTA_HEADROOM": "0.90",
            }
        )
        snap = quota()
        check("openai spendable is 90% of 250k", snap["openai_spendable"], 225000)
        check("openai_mini spendable is 90% of 2.5M", snap["openai_mini_spendable"], 2250000)
        check("deepseek spendable is 90% of 20M", snap["deepseek_spendable"], 18000000)

        print("\n=== preseed marks today's grant as already spent ===")
        proc.terminate(); proc.wait(timeout=10)
        rm_state()
        seen_models.clear(); seen_deepseek_models.clear()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        proc = start_proxy({"QUOTA_PRESEED_DAY": today, "QUOTA_PRESEED_OPENAI": str(OPENAI_BUDGET)})
        check("preseed applied to openai", quota()["openai_used"], OPENAI_BUDGET)
        check("preseed left openai_mini alone", quota()["openai_mini_used"], 0)
        call()
        check("first call already overflowed to deepseek", seen_deepseek_models[-1], "deepseek-v4-flash")

        print("\n=== preseed for another day is ignored ===")
        proc.terminate(); proc.wait(timeout=10)
        rm_state()
        proc = start_proxy({"QUOTA_PRESEED_DAY": "2020-01-01", "QUOTA_PRESEED_OPENAI": "1000"})
        check("stale preseed not applied", quota()["openai_used"], 0)

        print("\n=== saved state beats preseed ===")
        call()
        check("call recorded", quota()["openai_used"], 400)
        proc.terminate(); proc.wait(timeout=10)
        proc = start_proxy({"QUOTA_PRESEED_DAY": today, "QUOTA_PRESEED_OPENAI": "999"})
        check("saved state wins", quota()["openai_used"], 400)

        # A request reserves its estimated cost while in flight, so parallel
        # requests can't each be told the same budget is free. Before that, the
        # budget was only charged after a response arrived, so all 12 below would
        # read used=0 and be admitted to openai — the check was simply inert
        # under concurrency, and a burst of costlier calls could overshoot the
        # cap into billable usage. This is the race pr-agent flagged on PR #2.
        print("\n=== concurrent requests cannot collectively overshoot ===")
        global RESPONSE_DELAY
        proc.terminate(); proc.wait(timeout=10)
        rm_state()
        seen_models.clear(); seen_deepseek_models.clear()
        RESPONSE_DELAY = 1.0  # hold every call open so all 12 overlap
        # Pin the reservation to exactly 2500 so admissions are arithmetic:
        # admit while committed + 2500 <= 10000, i.e. at 0/2500/5000/7500.
        proc = start_proxy(
            {
                "OPENAI_DAILY_TOKENS": "10000",
                "DEEPSEEK_DAILY_TOKENS": "1000000",
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
        check("all 12 requests answered", len(results), 12)
        check("none rejected", [r for r in results if r != 200], [])
        check("only 4 admitted to openai", len(seen_models), 4)
        check("the rest overflowed to deepseek", len(seen_deepseek_models), 8)
        check("openai reservations never exceeded budget", snap["openai_used"] <= 10000, True)
        check("openai used exactly 4 calls' worth", snap["openai_used"], 4 * TOKENS_PER_CALL)
        check("deepseek charged the other 8", snap["deepseek_used"], 8 * TOKENS_PER_CALL)
        check("openai reservations all released", snap["openai_reserved"], 0)
        check("deepseek reservations all released", snap["deepseek_reserved"], 0)

        print("\n=== a reservation too big for the tier is not admitted ===")
        proc.terminate(); proc.wait(timeout=10)
        rm_state()
        seen_models.clear(); seen_deepseek_models.clear()
        # One request whose estimate alone exceeds the openai budget must not be
        # sent on openai just because the balance is still zero.
        proc = start_proxy(
            {
                "OPENAI_DAILY_TOKENS": "5000",
                "DEEPSEEK_DAILY_TOKENS": "1000000",
                "QUOTA_HEADROOM": "1.0",
                "QUOTA_RESERVE_MIN": "9000",
                "QUOTA_RESERVE_COMPLETION": "0",
            }
        )
        status, _ = call()
        check("oversized request overflowed, not admitted to openai", seen_deepseek_models[-1], "deepseek-v4-flash")
        check("openai untouched", quota()["openai_used"], 0)

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
        check("nothing metered for a failed call", snap["openai_used"], 0)
        check("reservation released, not leaked", snap["openai_reserved"], 0)
        # Budget must be fully usable again after the failure.
        seen_models.clear()
        status, _ = call()
        check("next call still admitted to openai", seen_models[-1], "gpt-5.4-2026-03-05")

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
        check("usage chunk metered", snap["openai_used"], TOKENS_PER_CALL)
        check("no reservation left held", snap["openai_reserved"], 0)

        # A success with no readable usage must not be free, or repeating it
        # (e.g. stream: true without stream_options.include_usage) would walk
        # past the daily caps. Charge the pre-flight estimate instead.
        print("\n=== stream with no usage chunk falls back to the estimate ===")
        proc.terminate(); proc.wait(timeout=10)
        rm_state()
        # Pin the estimate to exactly 777 so the fallback charge is checkable.
        proc = start_proxy({"QUOTA_RESERVE_MIN": "777", "QUOTA_RESERVE_COMPLETION": "0"})
        FORCE_STREAM = "nousage"
        status, _ = call()
        FORCE_STREAM = None
        snap = quota()
        check("stream relayed", status, 200)
        check("charged the estimate, not zero", snap["openai_used"], 777)
        check("reservation still released", snap["openai_reserved"], 0)

        print("\n=== non-streamed 200 with no usage field also falls back ===")
        proc.terminate(); proc.wait(timeout=10)
        rm_state()
        proc = start_proxy({"QUOTA_RESERVE_MIN": "777", "QUOTA_RESERVE_COMPLETION": "0"})
        global FORCE_NO_USAGE
        FORCE_NO_USAGE = True
        status, _ = call()
        FORCE_NO_USAGE = False
        snap = quota()
        check("response relayed", status, 200)
        check("charged the estimate", snap["openai_used"], 777)

        print("\n=== repeated unreadable responses still hit the openai_mini cap ===")
        proc.terminate(); proc.wait(timeout=10)
        rm_state()
        # 777-token estimate against a 2000 budget: admits at 0 and 777 and 1554
        # is over, so the third call must be refused rather than run free forever.
        # openai_mini has no downgrade target, so this is a clean single-bucket check.
        proc = start_proxy(
            {
                "OPENAI_MINI_DAILY_TOKENS": "2000",
                "QUOTA_HEADROOM": "1.0",
                "QUOTA_RESERVE_MIN": "777",
                "QUOTA_RESERVE_COMPLETION": "0",
            }
        )
        FORCE_NO_USAGE = True
        codes = [call(model="gpt-4.1-mini")[0] for _ in range(6)]
        FORCE_NO_USAGE = False
        snap = quota()
        check("unreadable calls eventually refused", 429 in codes, True)
        check("openai_mini charged despite no usage", snap["openai_mini_used"] > 0, True)
        check("openai_mini stayed within budget", snap["openai_mini_used"] <= 2000, True)

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

        # A negative length used to be accepted and treated as bodyless, leaving
        # the real body bytes unread to be parsed as the next request.
        for bad_len in ("-1", "-4096"):
            req = urllib.request.Request(
                f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
                data=b'{"model": "gpt-5.4-2026-03-05"}',
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            req.add_header("Content-Length", bad_len)
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    got = r.status
            except urllib.error.HTTPError as e:
                got = e.code
            except Exception as exc:
                got = f"raised {type(exc).__name__}"
            check(f"Content-Length {bad_len} rejected", got, 400)

        print("\n=== unreachable openai upstream answers 503, not a dropped connection ===")
        proc.terminate(); proc.wait(timeout=10)
        rm_state()
        dead_port = free_port()  # nothing is listening here
        proc = start_proxy({"OPENAI_UPSTREAM_BASE": f"http://127.0.0.1:{dead_port}"})
        status, resp = call()
        check("got a real status code", status, 503)
        check("error code identifies the cause", resp["error"]["code"], "upstream_unreachable")
        snap = quota()
        check("nothing metered", snap["openai_used"], 0)
        check("reservation released", snap["openai_reserved"], 0)
    finally:
        proc.terminate()
        try:
            out = proc.stdout.read() if proc.stdout else ""
        except Exception:
            out = ""
        openai_srv.shutdown()
        deepseek_srv.shutdown()

    if failures:
        print(f"\n--- proxy log (last run) ---\n{out[-1500:]}")
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
