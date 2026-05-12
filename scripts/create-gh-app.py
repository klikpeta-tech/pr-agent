#!/usr/bin/env python3
"""
Creates the klikpeta-pr-agent GitHub App using the App Manifest flow.

Usage:
    python scripts/create-gh-app.py

Opens a browser to the GitHub App creation page with all settings pre-filled.
Click "Create GitHub App" once, then this script captures and saves the credentials.
Credentials are saved to ~/.config/klikpeta/pr-agent-credentials.json (outside the repo).
"""

import json
import secrets
import webbrowser
import urllib.parse
import http.client
import os
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

ORG = "klikpeta-tech"
APP_NAME = "klikpeta-pr-agent"
SERVER_PORT = 3001
CALLBACK_URL = f"http://localhost:{SERVER_PORT}/callback"
FLY_HOST = "klikpeta-pr-agent.fly.dev"
CREDENTIALS_PATH = Path.home() / ".config" / "klikpeta" / "pr-agent-credentials.json"

MANIFEST = {
    "name": APP_NAME,
    "url": f"https://{FLY_HOST}",
    "hook_attributes": {
        "url": f"https://{FLY_HOST}/api/v1/github_webhooks"
    },
    "redirect_url": CALLBACK_URL,
    "public": False,
    "default_permissions": {
        "pull_requests": "write",
        "issues": "write",
        "metadata": "read",
        "contents": "read"
    },
    "default_events": [
        "issue_comment",
        "pull_request",
        "push"
    ]
}

credentials = {}


class AppServer(HTTPServer):
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    manifest_json: str = ""
    state: str = ""

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        if path == "/":
            # Serve an HTML page that auto-POSTs the manifest form to GitHub
            html = f"""<!DOCTYPE html>
<html><body style="font-family:monospace;padding:2em">
<p>Submitting app manifest to GitHub...</p>
<form id="f" method="POST"
      action="https://github.com/organizations/{ORG}/settings/apps/new">
  <input type="hidden" name="manifest" value="{self.manifest_json}">
  <input type="hidden" name="state"    value="{self.state}">
  <button type="submit">Click here if not redirected automatically</button>
</form>
<script>document.getElementById('f').submit();</script>
</body></html>"""
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/callback":
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code = params.get("code", [None])[0]

            if not code:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Missing code - please try again.")
                return

            # Exchange one-time code for app credentials
            conn = http.client.HTTPSConnection("api.github.com")
            conn.request(
                "POST",
                f"/app-manifests/{code}/conversions",
                headers={
                    "Accept": "application/vnd.github.v3+json",
                    "User-Agent": "klikpeta-pr-agent-setup/1.0"
                }
            )
            resp = conn.getresponse()
            data = json.loads(resp.read().decode())

            if "id" not in data:
                body = f"GitHub returned an error: {data}".encode()
                self.send_response(500)
                self.end_headers()
                self.wfile.write(body)
                return

            credentials.update(data)

            html = f"""<!DOCTYPE html><html><body style="font-family:monospace;padding:2em;max-width:600px">
<h2>GitHub App created!</h2>
<p><b>App ID:</b> {data['id']}</p>
<p><b>App name:</b> {data['name']}</p>
<p><b>URL:</b> <a href="{data['html_url']}">{data['html_url']}</a></p>
<hr>
<p>Credentials saved. Return to the terminal for next steps.</p>
<p>You can close this tab.</p>
</body></html>"""
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_):
        pass


def main():
    state = secrets.token_hex(16)
    manifest_json = json.dumps(MANIFEST).replace('"', "&quot;")

    Handler.manifest_json = manifest_json
    Handler.state = state

    print(f"Opening browser to create the '{APP_NAME}' GitHub App in org '{ORG}'...")
    print(f"It will auto-submit. Click 'Create GitHub App' on the GitHub page to confirm.\n")
    webbrowser.open(f"http://localhost:{SERVER_PORT}/")

    print(f"Waiting for GitHub callback on http://localhost:{SERVER_PORT} ...")
    server = AppServer(("localhost", SERVER_PORT), Handler)
    # Loop until the callback populates credentials (ignores favicon/extra requests)
    while not credentials:
        server.handle_request()

    if not credentials:
        print("No credentials received. Exiting.")
        sys.exit(1)

    # Save credentials outside the repo (never commit these)
    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_PATH.chmod(0o700) if CREDENTIALS_PATH.parent.exists() else None
    output = {
        "app_id": credentials["id"],
        "client_id": credentials["client_id"],
        "webhook_secret": credentials["webhook_secret"],
        "private_key": credentials["pem"],
        "app_name": credentials["name"],
        "html_url": credentials["html_url"],
        "install_url": f"https://github.com/organizations/{ORG}/settings/apps/{credentials['slug']}/installations"
    }
    CREDENTIALS_PATH.write_text(json.dumps(output, indent=2))
    CREDENTIALS_PATH.chmod(0o600)

    print(f"\n✅  App created!")
    print(f"    App ID  : {output['app_id']}")
    print(f"    App URL : {output['html_url']}")
    print(f"    Saved to: {CREDENTIALS_PATH}")
    print(f"\n── Next: install the app org-wide ──────────────────────────────")
    print(f"  Open this URL and select 'All repositories':")
    print(f"  {output['install_url']}")
    print(f"\n── Then: set Fly.io secrets ────────────────────────────────────")
    print(f"  python scripts/set-fly-secrets.py")


if __name__ == "__main__":
    main()
