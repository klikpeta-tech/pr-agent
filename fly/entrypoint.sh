#!/bin/bash
set -euo pipefail

# Seconds to wait for any one process to start listening before giving up.
READY_TIMEOUT="${READY_TIMEOUT:-90}"

QUOTA_PID=""
PR_AGENT_PID=""
PROXY_PID=""

stop_all() {
    local pid
    for pid in "$QUOTA_PID" "$PR_AGENT_PID" "$PROXY_PID"; do
        if [ -n "$pid" ]; then
            kill "$pid" 2>/dev/null || true
        fi
    done
}

# Block until $port accepts a TCP connection. Bails out early if the process
# backing it dies first, so a crash on boot doesn't cost the whole timeout.
#
# The probe is Python rather than bash's /dev/tcp: /dev/tcp is a compile-time
# bash feature, and if this image's bash lacked it every probe would fail and the
# container would crash-loop. Python is certainly present — it runs all three
# services below. Exit codes: 0 listening, 2 process died, 1 timed out.
wait_for_port() {
    local name="$1" port="$2" pid="$3"
    local rc=0

    python - "$port" "$pid" "$READY_TIMEOUT" <<'PY' || rc=$?
import os, socket, sys, time

port, pid, timeout = int(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3])
deadline = time.monotonic() + timeout
while time.monotonic() < deadline:
    try:
        os.kill(pid, 0)
    except OSError:
        sys.exit(2)
    with socket.socket() as sock:
        sock.settimeout(1)
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            sys.exit(0)
    time.sleep(0.5)
sys.exit(1)
PY

    case "$rc" in
        0) echo "[entrypoint] $name is listening on :$port"; return 0 ;;
        2) echo "[entrypoint] ERROR: $name (pid $pid) exited before binding :$port"; return 1 ;;
        *) echo "[entrypoint] ERROR: $name never bound :$port within ${READY_TIMEOUT}s"; return 1 ;;
    esac
}

# Start the OpenAI quota proxy first (localhost:3002) — pr-agent is configured to
# send every LLM call through it, so it must be listening before pr-agent starts.
echo "[entrypoint] Starting OpenAI quota proxy on port 3002..."
QUOTA_PROXY_PORT=3002 python /app/openai-quota-proxy.py &
QUOTA_PID=$!
wait_for_port "quota proxy" 3002 "$QUOTA_PID" || { stop_all; exit 1; }

# Start pr-agent on the internal port (3001).
echo "[entrypoint] Starting pr-agent on port 3001..."
PORT=3001 python -m pr_agent.servers.github_app &
PR_AGENT_PID=$!

# Wait for pr-agent to actually bind — it takes ~8s, and starting the public
# proxy before then means the first request gets a 502. That matters because Fly
# suspends this app when idle (min_machines_running = 0), so an incoming GitHub
# webhook is usually what wakes it: a 502 here is a failed webhook delivery.
wait_for_port "pr-agent" 3001 "$PR_AGENT_PID" || { stop_all; exit 1; }

echo "[entrypoint] Starting auto-approve proxy on port 3000..."
PORT=3000 UPSTREAM_PORT=3001 python /app/auto-approve-proxy.py &
PROXY_PID=$!
wait_for_port "auto-approve proxy" 3000 "$PROXY_PID" || { stop_all; exit 1; }

echo "[entrypoint] All processes up — ready to serve on :3000"

# If any process dies, take down the others and exit so Fly restarts the container.
wait_any() {
    while true; do
        if ! kill -0 "$PR_AGENT_PID" 2>/dev/null; then
            echo "[entrypoint] pr-agent exited, shutting down."
            stop_all
            exit 1
        fi
        if ! kill -0 "$PROXY_PID" 2>/dev/null; then
            echo "[entrypoint] proxy exited, shutting down."
            stop_all
            exit 1
        fi
        # Without the quota proxy every LLM call would fail anyway, and letting
        # pr-agent run on would mean unmetered (billable) traffic if its api_base
        # were ever relaxed — so treat this as fatal too.
        if ! kill -0 "$QUOTA_PID" 2>/dev/null; then
            echo "[entrypoint] quota proxy exited, shutting down."
            stop_all
            exit 1
        fi
        sleep 5
    done
}

wait_any
