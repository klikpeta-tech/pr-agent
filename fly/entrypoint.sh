#!/bin/bash
set -euo pipefail

# Start pr-agent on the internal port (3001).
# The proxy listens on the public port (3000) and forwards to it.
echo "[entrypoint] Starting pr-agent on port 3001..."
PORT=3001 python -m pr_agent.servers.github_app &
PR_AGENT_PID=$!

# Give pr-agent a moment to bind before the proxy starts accepting connections.
sleep 2

echo "[entrypoint] Starting auto-approve proxy on port 3000..."
PORT=3000 UPSTREAM_PORT=3001 python /app/auto-approve-proxy.py &
PROXY_PID=$!

# If either process dies, take down the other and exit so Fly restarts the container.
wait_any() {
    while true; do
        if ! kill -0 "$PR_AGENT_PID" 2>/dev/null; then
            echo "[entrypoint] pr-agent exited, shutting down."
            kill "$PROXY_PID" 2>/dev/null || true
            exit 1
        fi
        if ! kill -0 "$PROXY_PID" 2>/dev/null; then
            echo "[entrypoint] proxy exited, shutting down."
            kill "$PR_AGENT_PID" 2>/dev/null || true
            exit 1
        fi
        sleep 5
    done
}

wait_any
