#!/usr/bin/env python3
"""Add the QOS MCP server to Claude Desktop on this Mac (no menus needed).

    python3 deploy/setup_claude_desktop.py              asks for the assistant operator PIN (not shown while typing)
    python3 deploy/setup_claude_desktop.py --remove     takes it out again

Writes ~/Library/Application Support/Claude/claude_desktop_config.json, keeps every other server in it, and saves a
backup next to it first. The PIN is stored in that file on this Mac only. Claude Desktop runs mcp_server.py from this
repo; it talks to the engine at BOT_API (default 127.0.0.1:8000, i.e. through the SSH tunnel). Restart Claude Desktop after.
"""
from __future__ import annotations

import argparse
import getpass
import json
import shutil
import sys
import time
from pathlib import Path

CFG = Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
BOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="qos", help="server name shown in Claude (default qos)")
    ap.add_argument("--operator", default="assistant", help="operator id with role 'assistant' (default assistant)")
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--remove", action="store_true")
    a = ap.parse_args()

    doc = {}
    if CFG.exists():
        try:
            doc = json.loads(CFG.read_text() or "{}")
        except json.JSONDecodeError as e:
            print(f"{CFG} is not valid JSON ({e}); fix or move it first")
            return 1
        backup = CFG.with_name(f"claude_desktop_config.backup-{time.strftime('%Y%m%d-%H%M%S')}.json")
        shutil.copy2(CFG, backup)
        print(f"backup: {backup}")
    servers = doc.setdefault("mcpServers", {})

    if a.remove:
        servers.pop(a.name, None)
    else:
        uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
        if not Path(uv).exists():
            print("uv not found: install it (curl -LsSf https://astral.sh/uv/install.sh | sh) and run this again")
            return 1
        pin = getpass.getpass(f"PIN of operator '{a.operator}': ")
        if not pin:
            print("no PIN given, nothing changed")
            return 1
        servers[a.name] = {
            "command": uv,
            "args": ["run", "--directory", str(BOT), "mcp_server.py"],
            "env": {"BOT_API": a.api, "ASSISTANT_OPERATOR_ID": a.operator, "ASSISTANT_PIN": pin},
        }

    CFG.parent.mkdir(parents=True, exist_ok=True)
    CFG.write_text(json.dumps(doc, indent=2) + "\n")
    CFG.chmod(0o600)
    print(("removed" if a.remove else "added") + f" MCP server '{a.name}' in {CFG}")
    print("Quit Claude Desktop completely (Cmd+Q) and open it again.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
