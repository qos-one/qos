"""
Check the remote MCP endpoint the way an AI provider calls it (Streamable HTTP + bearer token), from any computer.
Lists the tools and reads get_status. Sends no order.

  export QOS_MCP_URL=https://mcp.example.com/mcp
  export QOS_MCP_TOKEN=$(ssh qos@SERVER "grep '^MCP_BEARER_TOKEN=' /srv/qos/home/.env | cut -d= -f2")
  uv run python scripts/check_remote_mcp.py
"""
from __future__ import annotations

import json
import os
import sys

import httpx


def rpc(c: httpx.Client, url: str, headers: dict, method: str, params: dict | None = None, rid: int | None = 1) -> dict | None:
    body = {"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {}), **({"id": rid} if rid else {})}
    r = c.post(url, headers=headers, json=body)
    if r.status_code == 401:
        raise SystemExit("401 unauthorized: wrong or missing QOS_MCP_TOKEN")
    r.raise_for_status()
    if sid := r.headers.get("mcp-session-id"):
        headers["mcp-session-id"] = sid
    if not rid:
        return None
    text = r.text
    if r.headers.get("content-type", "").startswith("text/event-stream"):
        text = next((ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")), "{}")
    return json.loads(text)


def main() -> int:
    url, token = os.getenv("QOS_MCP_URL", ""), os.getenv("QOS_MCP_TOKEN", "")
    if not url or not token:
        print(__doc__)
        return 2
    h = {"authorization": f"Bearer {token}", "accept": "application/json, text/event-stream"}
    with httpx.Client(timeout=30) as c:
        init = rpc(c, url, h, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                             "clientInfo": {"name": "qos-check", "version": "1"}})
        print("server:", init.get("result", {}).get("serverInfo"))
        rpc(c, url, h, "notifications/initialized", rid=None)
        tools = rpc(c, url, h, "tools/list", {}, rid=2).get("result", {}).get("tools", [])
        print(f"tools ({len(tools)}):", ", ".join(t["name"] for t in tools))
        res = rpc(c, url, h, "tools/call", {"name": "get_status", "arguments": {}}, rid=3).get("result", {})
        content = (res.get("content") or [{}])[0].get("text", "")
        try:
            st = json.loads(content)
            health = st.get("health", st)
            if "error" in health:
                print("get_status error:", health["error"])
                return 1
            print("get_status: ib_connected =", health.get("ib_connected"), "· mode =", health.get("mode"),
                  "· assistant write_enabled =", (st.get("assistant_permissions") or {}).get("write_enabled"))
        except Exception:
            print("get_status:", content[:300])
    return 0


if __name__ == "__main__":
    sys.exit(main())
