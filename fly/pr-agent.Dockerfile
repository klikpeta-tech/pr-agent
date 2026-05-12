FROM pragent/pr-agent:latest

COPY fly/pr-agent-override.toml /app/pr_agent/settings/.secrets.toml
COPY fly/auto-approve-proxy.py   /app/auto-approve-proxy.py
COPY fly/entrypoint.sh           /app/entrypoint.sh

RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
