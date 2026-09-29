#!/usr/bin/env python3
"""
Tests the clean-review detection in fly/auto-approve-proxy.py.

Run with:  python scripts/test-auto-approve-proxy.py

No dependencies and no network access. The fixtures are real comment bodies:
CLEAN and WITH_FINDINGS from klikpeta-tech/pr-agent#2 (a review with findings
and the same persistent comment after the findings were fixed), and
CLEAN_NO_FINDINGS_ROW from klikpeta-tech/bdf-dbt#1 (a review where the model
returned no findings at all, which renders as a structurally different row).

This logic decides whether the app submits an APPROVE that counts toward branch
protection, so a wrong "clean" verdict is the expensive direction: it approves a
PR that has real findings. Every ambiguous case below must come out False.
"""

import importlib.util
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROXY_SRC = os.path.join(REPO_ROOT, "fly", "auto-approve-proxy.py")

failures: list[str] = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r} want {want!r}")
    if not ok:
        failures.append(label)


# Real body of the persistent review once every finding was fixed: the
# focus-areas cell is emitted but empty.
CLEAN = """## PR Reviewer Guide 🔍

#### (Review updated until commit https://github.com/klikpeta-tech/pr-agent/commit/78f1ae5)


Here are some key observations to aid the review process:

<table>
<tr><td>⏱️&nbsp;<strong>Estimated effort to review</strong>: 4 🔵🔵🔵🔵⚪</td></tr>
<tr><td>🧪&nbsp;<strong>PR contains tests</strong></td></tr>
<tr><td>🔒&nbsp;<strong>No security concerns identified</strong></td></tr>
<tr><td>⚡&nbsp;<strong>Recommended focus areas for review</strong><br><br>

</td></tr>
</table>
"""

# Real body of a review where the model returned no findings at all — a
# structurally different clean shape than CLEAN above (captured from
# klikpeta-tech/bdf-dbt#1, comment 5790645315): pr-agent renders a dedicated
# "No major issues detected" row instead of an empty focus-areas cell.
CLEAN_NO_FINDINGS_ROW = """## PR Reviewer Guide 🔍

Here are some key observations to aid the review process:

<table>
<tr><td>⏱️&nbsp;<strong>Estimated effort to review</strong>: 4 🔵🔵🔵🔵⚪</td></tr>
<tr><td>🧪&nbsp;<strong>PR contains tests</strong></td></tr>
<tr><td>🔒&nbsp;<strong>No security concerns identified</strong></td></tr>
<tr><td>⚡&nbsp;<strong>No major issues detected</strong></td></tr>
</table>
"""

# Real body of the same review while it still had two findings.
WITH_FINDINGS = """## PR Reviewer Guide 🔍

Here are some key observations to aid the review process:

<table>
<tr><td>⏱️&nbsp;<strong>Estimated effort to review</strong>: 4 🔵🔵🔵🔵⚪</td></tr>
<tr><td>🧪&nbsp;<strong>PR contains tests</strong></td></tr>
<tr><td>🔒&nbsp;<strong>No security concerns identified</strong></td></tr>
<tr><td>⚡&nbsp;<strong>Recommended focus areas for review</strong><br><br>

<details><summary><a href='https://github.com/klikpeta-tech/pr-agent/pull/2/files#diff-b80148R257-R268'><strong>Oversized admission</strong></a>

The quota check only verifies that current `used + reserved` is below the cap
before admitting a request, but it does not verify that the new reservation
itself still fits.
</summary>

```python
    if self._committed_locked(1) < TIER_SPENDABLE[1]:
        self._reserved[1] += estimate
```
</details>

<details><summary><a href='https://github.com/klikpeta-tech/pr-agent/pull/2/files#diff-b80148R300'><strong>Midnight miscount</strong></a>

Requests that start before the daily rollover and finish after midnight are
charged to the new day.
</summary>
</details>

</td></tr>
</table>
"""

# A review with a real finding whose quoted content happens to include the
# exact no-issues-row markup verbatim — realistic for this repo specifically,
# since a review of auto-approve-proxy.py's own diff could easily quote this
# string as an example. Must not short-circuit to "clean": the finding, and
# the "Recommended focus areas" heading it lives under, are real.
QUOTED_NO_ISSUES_MARKUP_INSIDE_FINDING = """## PR Reviewer Guide 🔍

<table>
<tr><td>⚡&nbsp;<strong>Recommended focus areas for review</strong><br><br>

<details><summary><strong>False approval risk</strong></a>

If a finding's own quoted text contains
<td>⚡&nbsp;<strong>No major issues detected</strong></td>
verbatim, a naive substring search would misclassify this review as clean.
</summary></details>

</td></tr>
</table>
"""

spec = importlib.util.spec_from_file_location("aap", PROXY_SRC)
aap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aap)

print("=== real captured review bodies ===")
check("clean review (empty focus cell) is clean", aap.review_is_clean(CLEAN), True)
check("clean review (no-findings row) is clean", aap.review_is_clean(CLEAN_NO_FINDINGS_ROW), True)
check("review with 2 findings is not clean", aap.review_is_clean(WITH_FINDINGS), False)
check(
    "no-issues markup quoted inside a real finding is not clean",
    aap.review_is_clean(QUOTED_NO_ISSUES_MARKUP_INSIDE_FINDING),
    False,
)

print("\n=== a single finding must still block ===")
one_finding = CLEAN.replace(
    "for review</strong><br><br>\n\n</td>",
    "for review</strong><br><br>\n\n<details><summary><strong>Something</strong>"
    "\n\nA real problem.</summary></details>\n\n</td>",
)
check("one finding is not clean", aap.review_is_clean(one_finding), False)

print("\n=== ambiguous shapes must fail safe (False) ===")
check("focus-areas section missing entirely", aap.review_is_clean("## PR Reviewer Guide 🔍"), False)
check("empty body", aap.review_is_clean(""), False)
check(
    "heading present but cell never closed",
    aap.review_is_clean("Recommended focus areas for review</strong><br><br>"),
    False,
)
check(
    "bare prose in the cell (markup changed shape)",
    aap.review_is_clean(
        "<td>Recommended focus areas for review</strong><br><br>\n"
        "Potential race condition in the counter\n</td>"
    ),
    False,
)
check(
    "the old prose phrase alone no longer approves",
    aap.review_is_clean("## PR Reviewer Guide 🔍\n\nNo major issues detected"),
    False,
)

print("\n=== whitespace-only variations are still clean ===")
for label, cell in [
    ("newlines only", "\n\n\n"),
    ("nbsp only", "&nbsp;"),
    ("br tags only", "<br><br>"),
    ("nothing at all", ""),
]:
    body = f"<td>⚡&nbsp;<strong>Recommended focus areas for review</strong>{cell}</td>"
    check(f"cell with {label}", aap.review_is_clean(body), True)

print("\n=== the old trigger constant is gone ===")
check("APPROVAL_TRIGGER removed", hasattr(aap, "APPROVAL_TRIGGER"), False)

# ── webhook handling over real HTTP ─────────────────────────────────────────
# Regression cover for 2026-09-29: pr-agent was slow to answer, GitHub gave up
# after 10s ("context deadline exceeded ... awaiting headers"), and because the
# approve/request-changes decision ran only after writing pr-agent's reply back,
# the dropped connection skipped it and the PR never got a verdict.
print("\n=== webhook: acknowledged fast, decision independent of pr-agent ===")

import hashlib
import hmac
import http.server
import json
import socket
import struct
import threading
import time
import urllib.request

upstream_delay = [0.0]
upstream_seen: list[tuple[str, bytes]] = []


class FakePrAgent(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        time.sleep(upstream_delay[0])
        upstream_seen.append((self.path, body))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(cond, timeout=6.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


upstream_port = free_port()
upstream_srv = http.server.ThreadingHTTPServer(("127.0.0.1", upstream_port), FakePrAgent)
threading.Thread(target=upstream_srv.serve_forever, daemon=True).start()
aap.UPSTREAM = f"http://127.0.0.1:{upstream_port}"
aap.UPSTREAM_PORT = upstream_port
aap.WEBHOOK_SECRET = b"test-secret"
aap.FORWARD_RETRY_DELAY = 0.05

actions: list[tuple[str, dict]] = []
aap._maybe_auto_action = lambda event, payload: actions.append((event, payload))

proxy_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), aap._ProxyHandler)
proxy_port = proxy_srv.server_address[1]
threading.Thread(target=proxy_srv.serve_forever, daemon=True).start()

PAYLOAD = json.dumps({"action": "edited", "comment": {"id": 1}}).encode()


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()


def post(body: bytes = PAYLOAD, signature: str | None = None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{proxy_port}/api/v1/github_webhooks",
        data=body,
        method="POST",
        headers={
            "X-GitHub-Event": "issue_comment",
            "X-Hub-Signature-256": signature or sign(body),
        },
    )
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, time.monotonic() - start
    except urllib.error.HTTPError as exc:
        return exc.code, time.monotonic() - start


def reset():
    upstream_seen.clear()
    actions.clear()
    upstream_delay[0] = 0.0


# 1. A slow pr-agent must not hold up the reply to GitHub.
reset()
upstream_delay[0] = 2.0
status, elapsed = post()
check("slow pr-agent: webhook acknowledged with 200", status, 200)
check("slow pr-agent: acknowledged in under 1s", elapsed < 1.0, True)
check("slow pr-agent: decision already made at ack time", len(actions), 1)
check("slow pr-agent: decision sees the event and payload", actions[0], ("issue_comment", json.loads(PAYLOAD)))
check("slow pr-agent: webhook still reaches pr-agent afterwards", wait_for(lambda: len(upstream_seen) == 1), True)
check("slow pr-agent: forwarded byte-for-byte", upstream_seen[0], ("/api/v1/github_webhooks", PAYLOAD))

# 2. GitHub hanging up before it reads the reply must not skip the decision.
reset()
conn = socket.create_connection(("127.0.0.1", proxy_port))
conn.sendall(
    b"POST /api/v1/github_webhooks HTTP/1.1\r\nHost: x\r\n"
    b"X-GitHub-Event: issue_comment\r\n"
    + f"X-Hub-Signature-256: {sign(PAYLOAD)}\r\n".encode()
    + f"Content-Length: {len(PAYLOAD)}\r\n\r\n".encode()
    + PAYLOAD
)
# RST on close, so the proxy's write fails the way it does after a GitHub timeout.
conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
conn.close()
check("client hung up: decision still made", wait_for(lambda: len(actions) == 1), True)
check("client hung up: webhook still forwarded to pr-agent", wait_for(lambda: len(upstream_seen) == 1), True)
status, _ = post()
check("client hung up: proxy keeps serving afterwards", status, 200)
# Its forward is asynchronous too; let it land before the next case resets.
wait_for(lambda: len(upstream_seen) == 2)

# 3. A bad signature is rejected before anything runs.
reset()
status, _ = post(signature="sha256=" + "0" * 64)
check("bad signature: 401", status, 401)
time.sleep(0.3)
check("bad signature: no decision", actions, [])
check("bad signature: nothing forwarded", upstream_seen, [])

# 4. An unreachable pr-agent is retried a bounded number of times, and the
#    ack and the decision don't depend on it.
reset()
attempts: list[int] = []
real_upstream_request = aap._upstream_request


def unreachable(method, path, headers, body=None):
    attempts.append(1)
    return 503, b"", False


aap._upstream_request = unreachable
status, _ = post()
check("pr-agent down: still acknowledged with 200", status, 200)
check("pr-agent down: decision still made", len(actions), 1)
check("pr-agent down: retried, then gave up", wait_for(lambda: len(attempts) == aap.FORWARD_ATTEMPTS), True)
time.sleep(0.3)
check("pr-agent down: no attempts beyond the limit", len(attempts), aap.FORWARD_ATTEMPTS)
aap._upstream_request = real_upstream_request

# 5. An HTTP error from pr-agent is final, not replayed.
reset()
attempts.clear()


def erroring(method, path, headers, body=None):
    attempts.append(1)
    return 500, b"", True


aap._upstream_request = erroring
post()
time.sleep(0.3)
check("pr-agent HTTP error: not retried", len(attempts), 1)
aap._upstream_request = real_upstream_request

# 6. Health checks still go through to pr-agent, and don't traceback when the
#    prober has already given up.
with urllib.request.urlopen(f"http://127.0.0.1:{proxy_port}/", timeout=5) as resp:
    check("GET / passes through to pr-agent", (resp.status, resp.read()), (200, b"ok"))
conn = socket.create_connection(("127.0.0.1", proxy_port))
conn.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
conn.close()
time.sleep(0.3)
with urllib.request.urlopen(f"http://127.0.0.1:{proxy_port}/", timeout=5) as resp:
    check("GET / after an abandoned probe still works", resp.status, 200)

proxy_srv.shutdown()
upstream_srv.shutdown()

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL PASS")
