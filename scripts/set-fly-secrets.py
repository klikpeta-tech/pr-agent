#!/usr/bin/env python3
"""
Sets pr-agent secrets on the Fly.io app directly from the .pem file.

Usage:
    python scripts/set-fly-secrets.py --pem ~/Downloads/klikpeta-pr-agent.*.pem
"""

import glob
import subprocess
import sys
from pathlib import Path

FLY_APP = "klikpeta-pr-agent"
APP_ID = "3686117"
WEBHOOK_SECRET = "7b24569ce792e329a035fe24816dbc9a788f30dd"


def find_pem(arg=None):
    if arg:
        matches = glob.glob(str(Path(arg).expanduser()))
        if matches:
            return Path(matches[0])
    # Try common download locations
    for pattern in [
        "~/Downloads/klikpeta-pr-agent.*.private-key.pem",
        "~/Downloads/klikpeta-pr-agent.*.pem",
        "~/Downloads/*.private-key.pem",
    ]:
        matches = glob.glob(str(Path(pattern).expanduser()))
        if matches:
            return Path(matches[0])
    return None


def main():
    pem_arg = None
    if "--pem" in sys.argv:
        idx = sys.argv.index("--pem")
        if idx + 1 < len(sys.argv):
            pem_arg = sys.argv[idx + 1]

    pem_path = find_pem(pem_arg)
    if not pem_path or not pem_path.exists():
        print("Could not find the .pem private key file.")
        print("Usage: python scripts/set-fly-secrets.py --pem ~/Downloads/klikpeta-pr-agent.*.pem")
        sys.exit(1)

    print(f"Using private key: {pem_path}")
    private_key = pem_path.read_text().strip().replace("\n", "\\n")

    openai_key = input("Enter your OpenAI API key (or press Enter to skip): ").strip()

    secrets = {
        "GITHUB__APP_ID": APP_ID,
        "GITHUB__WEBHOOK_SECRET": WEBHOOK_SECRET,
        "GITHUB__PRIVATE_KEY": private_key,
    }
    if openai_key:
        secrets["OPENAI__KEY"] = openai_key

    print(f"\nSetting {len(secrets)} secrets on Fly app '{FLY_APP}'...")
    cmd = ["fly", "secrets", "set", "--app", FLY_APP] + [f"{k}={v}" for k, v in secrets.items()]
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print("flyctl error:")
        print(result.stderr)
        sys.exit(1)

    print("✅  Secrets set.")
    print("\n── Deploy ──────────────────────────────────────────────────────")
    print(f"  fly deploy --config fly/fly.pr-agent.toml --app {FLY_APP}")
    print("\n── Install app org-wide (if not done yet) ──────────────────────")
    print(f"  https://github.com/apps/klikpeta-pr-agent/installations/new")


if __name__ == "__main__":
    main()
