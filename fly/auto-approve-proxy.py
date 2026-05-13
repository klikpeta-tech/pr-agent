#!/usr/bin/env python3
"""
Reverse proxy that sits in front of pr-agent.

Forwards all webhook requests to pr-agent (upstream on port 3001), then checks
whether the incoming event is an issue_comment from the pr-agent bot. Acts on
the review result:
  - "No major issues detected" → submits a formal GitHub PR approval.
  - Issues detected (pr-agent review comment without the above phrase) →
    submits REQUEST_CHANGES and adds REVIEWER_USERNAME as a requested reviewer.

Auth:
  All review actions (approve, request changes, add reviewer) use the GitHub
  App installation token. The App must have contents:write permission so its
  reviews count toward branch protection required approvals.

Other environment variables:
  GITHUB__WEBHOOK_SECRET   Used to verify incoming webhook signatures
  PR_AGENT_BOT_LOGIN       Exact GitHub login of the pr-agent bot (default: klikpeta-pr-agent[bot])
  REVIEWER_USERNAME        GitHub login to assign as reviewer when issues are found (default: mfhanif)
  PORT          Port this proxy listens on  (default 3000)
  UPSTREAM_PORT Port pr-agent listens on   (default 3001)
"""

import hashlib
import hmac
import http.server
import json
import os
import threading
import urllib.error
import urllib.request

LISTEN_PORT = int(os.environ.get("PORT", "3000"))
UPSTREAM_PORT = int(os.environ.get("UPSTREAM_PORT", "3001"))
UPSTREAM = f"http://127.0.0.1:{UPSTREAM_PORT}"

_secret = os.environ.get("GITHUB__WEBHOOK_SECRET", "")
WEBHOOK_SECRET: bytes = _secret.encode() if _secret else b""

APP_ID = os.environ.get("GITHUB__APP_ID", "")
PRIVATE_KEY = os.environ.get("GITHUB__PRIVATE_KEY", "").replace("\\n", "\n")
BOT_PAT = os.environ.get("GITHUB__BOT_PAT", "")

APPROVAL_TRIGGER = "No major issues detected"
# pr-agent review comments always contain this header; guards against acting on
# /describe or /improve bot comments.
REVIEW_COMMENT_MARKER = "PR Reviewer Guide"
# Pin to the exact bot login so a different GitHub App can't spoof the trigger.
PR_AGENT_BOT_LOGIN = os.environ.get("PR_AGENT_BOT_LOGIN", "klikpeta-pr-agent[bot]")
REVIEWER_USERNAME = os.environ.get("REVIEWER_USERNAME", "mfhanif")


# ── JWT / GitHub token helpers ────────────────────────────────────────────────

def _generate_app_jwt() -> str:
    import time
    import jwt  # PyJWT – already a pr-agent dependency

    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 600, "iss": APP_ID}
    return jwt.encode(payload, PRIVATE_KEY, algorithm="RS256")


def _get_installation_token(installation_id: int) -> str:
    app_jwt = _generate_app_jwt()
    req = urllib.request.Request(
        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        method="POST",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())["token"]


# ── Auto-approve logic ────────────────────────────────────────────────────────

def _approve_pr(owner: str, repo: str, pull_number: int, installation_id: int) -> None:
    try:
        token = _get_installation_token(installation_id)
        body = json.dumps(
            {"event": "APPROVE", "body": "Auto-approved: pr-agent found no major issues. ✅"}
        ).encode()
        req = urllib.request.Request(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pull_number}/reviews",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(req, timeout=15):
            pass
        print(f"[auto-approve] ✅ Approved PR #{pull_number} in {owner}/{repo}", flush=True)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        # 422 = already approved / can't approve own PR — not an error worth retrying
        if exc.code == 422:
            print(
                f"[auto-approve] ⚠️  PR #{pull_number} already approved or app can't self-approve"
                f" (HTTP 422): {detail}",
                flush=True,
            )
        else:
            print(
                f"[auto-approve] ❌ Failed to approve PR #{pull_number}: HTTP {exc.code} {detail}",
                flush=True,
            )
    except Exception as exc:
        print(f"[auto-approve] ❌ Failed to approve PR #{pull_number}: {exc}", flush=True)


def _request_changes_and_add_reviewer(
    owner: str, repo: str, pull_number: int, installation_id: int, pr_author: str
) -> None:
    try:
        token = _get_installation_token(installation_id)
        gh_headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        # 1. Add reviewer — skip if unset or if reviewer is the PR author
        if REVIEWER_USERNAME and REVIEWER_USERNAME != pr_author:
            reviewer_body = json.dumps({"reviewers": [REVIEWER_USERNAME]}).encode()
            req = urllib.request.Request(
                f"https://api.github.com/repos/{owner}/{repo}/pulls/{pull_number}/requested_reviewers",
                data=reviewer_body,
                method="POST",
                headers=gh_headers,
            )
            try:
                with urllib.request.urlopen(req, timeout=15):
                    pass
                print(
                    f"[auto-action] 👤 Added {REVIEWER_USERNAME} as reviewer on PR #{pull_number}"
                    f" in {owner}/{repo}",
                    flush=True,
                )
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")
                if exc.code == 422:
                    print(
                        f"[auto-action] ⚠️  Could not add reviewer on PR #{pull_number}"
                        f" (HTTP 422 — already added): {detail}",
                        flush=True,
                    )
                else:
                    print(
                        f"[auto-action] ❌ Failed to add reviewer on PR #{pull_number}:"
                        f" HTTP {exc.code} {detail}",
                        flush=True,
                    )
            except Exception as exc:
                # Catch network/timeout errors so REQUEST_CHANGES still runs below.
                print(
                    f"[auto-action] ❌ Failed to add reviewer on PR #{pull_number}: {exc}",
                    flush=True,
                )
        elif REVIEWER_USERNAME and REVIEWER_USERNAME == pr_author:
            print(
                f"[auto-action] ⏭️  Skipping reviewer assignment — {pr_author} is the PR author",
                flush=True,
            )

        # 2. Request changes
        review_body = json.dumps(
            {"event": "REQUEST_CHANGES", "body": "pr-agent found issues — review required. ⚠️"}
        ).encode()
        req = urllib.request.Request(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pull_number}/reviews",
            data=review_body,
            method="POST",
            headers=gh_headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=15):
                pass
            print(
                f"[auto-action] ⚠️  Requested changes on PR #{pull_number} in {owner}/{repo}",
                flush=True,
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            if exc.code == 422:
                print(
                    f"[auto-action] ⚠️  Could not request changes on PR #{pull_number}"
                    f" (HTTP 422 — can't review own PR): {detail}",
                    flush=True,
                )
            else:
                print(
                    f"[auto-action] ❌ Failed to request changes on PR #{pull_number}:"
                    f" HTTP {exc.code} {detail}",
                    flush=True,
                )
    except Exception as exc:
        print(f"[auto-action] ❌ Unexpected error on PR #{pull_number}: {exc}", flush=True)


def _maybe_auto_action(event_type: str, payload: dict) -> None:
    if event_type != "issue_comment":
        return
    # React to both 'created' (first review) and 'edited' (updated review on push)
    if payload.get("action") not in ("created", "edited"):
        return

    comment = payload.get("comment", {})
    issue = payload.get("issue", {})
    repo = payload.get("repository", {})
    installation = payload.get("installation", {})

    # Only PRs (not plain issues)
    if "pull_request" not in issue:
        return

    # Must be the exact pr-agent bot identity (not just any [bot] account)
    sender_login: str = comment.get("user", {}).get("login", "")
    if sender_login != PR_AGENT_BOT_LOGIN:
        return

    body: str = comment.get("body", "")

    # Only act on pr-agent review comments, not /describe or /improve output
    if REVIEW_COMMENT_MARKER not in body:
        return

    owner = repo.get("owner", {}).get("login", "")
    repo_name = repo.get("name", "")
    pull_number = issue.get("number")
    installation_id = installation.get("id")
    pr_author: str = issue.get("user", {}).get("login", "")

    if not all([owner, repo_name, pull_number, installation_id]):
        print("[auto-action] Missing required fields in payload, skipping.", flush=True)
        return

    if APPROVAL_TRIGGER in body:
        print(
            f"[auto-action] Approval trigger on PR #{pull_number} in {owner}/{repo_name}"
            f" (comment by {sender_login})",
            flush=True,
        )
        threading.Thread(
            target=_approve_pr,
            args=(owner, repo_name, pull_number, installation_id),
            daemon=True,
        ).start()
    else:
        print(
            f"[auto-action] Issues detected on PR #{pull_number} in {owner}/{repo_name}"
            f" (comment by {sender_login}, author {pr_author})",
            flush=True,
        )
        threading.Thread(
            target=_request_changes_and_add_reviewer,
            args=(owner, repo_name, pull_number, installation_id, pr_author),
            daemon=True,
        ).start()


# ── HTTP proxy handler ────────────────────────────────────────────────────────

# Headers that must not be forwarded verbatim
_HOP_BY_HOP = frozenset(
    ["connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
     "te", "trailers", "transfer-encoding", "upgrade"]
)


class _ProxyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # suppress default Apache-style log
        pass

    def _verify_signature(self, body: bytes) -> bool:
        if not WEBHOOK_SECRET:
            return True
        sig = self.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(WEBHOOK_SECRET, body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)

    def _forward(self, method: str, body: bytes | None = None) -> tuple[int, bytes]:
        url = UPSTREAM + self.path
        fwd_headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in _HOP_BY_HOP and k.lower() != "host"
        }
        if body is not None:
            fwd_headers["Content-Length"] = str(len(body))
        req = urllib.request.Request(url, data=body, headers=fwd_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        if not self._verify_signature(body):
            self.send_response(401)
            self.end_headers()
            return

        event_type = self.headers.get("X-GitHub-Event", "")

        status, resp_body = self._forward("POST", body)
        self.send_response(status)
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)

        # Inspect after forwarding (non-blocking)
        try:
            _maybe_auto_action(event_type, json.loads(body))
        except Exception as exc:
            print(f"[auto-action] Error inspecting payload: {exc}", flush=True)

    def do_GET(self):
        status, resp_body = self._forward("GET")
        self.send_response(status)
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), _ProxyHandler)
    print(f"[proxy] Listening on :{LISTEN_PORT} → upstream :{UPSTREAM_PORT}", flush=True)
    server.serve_forever()
