"""IB Gateway profiles: one container per (IBKR user, paper|live) — several can run at once and switch without a restart"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from qos import paths  # noqa: E402  (step 17: all file locations come from QOS_HOME)

DATA_DIR = paths.data_dir()
GW_FILE = DATA_DIR / "gateways.json"
IBGW_ROOT = paths.ibgw_dir()
IMAGE = "ghcr.io/gnzsnz/ib-gateway:stable"
BASE_API_PORT = 4010
BASE_VNC_PORT = 5902

COMPOSE_TMPL = """name: {project}
services:
  ib-gateway:
    image: {image}
    restart: unless-stopped
    env_file: .env
    environment:
      READ_ONLY_API: "no"
      EXISTING_SESSION_DETECTED_ACTION: primary
      TWOFA_TIMEOUT_ACTION: restart
      AUTO_RESTART_TIME: "11:55 PM"
      TIME_ZONE: America/New_York
    ports:
      - "127.0.0.1:{api_port}:{container_port}"
      - "127.0.0.1:{vnc_port}:5900"
"""


def _load() -> dict:
    try:
        return json.loads(GW_FILE.read_text())
    except Exception:
        return {"gateways": []}


def _save(doc: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    GW_FILE.write_text(json.dumps(doc, indent=2))


def read_env(path: Path) -> dict:
    out = {}
    try:
        for line in path.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                    inner = v[1:-1]
                    v = inner.replace("$$", "$").replace('\\"', '"').replace("\\\\", "\\") if v[0] == '"' else inner
                out[k.strip()] = v
    except Exception:
        pass
    return out


def write_env(path: Path, values: dict) -> None:
    cur = read_env(path)
    cur.update(values)

    def q(v: str) -> str:
        v = str(v)
        if v == "" or not any(ch in v for ch in " #$'\"\\\t"):
            return v
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$") + '"'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{k}={q(v)}\n" for k, v in cur.items()))
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def docker(args: list[str], cwd: Path, timeout: int = 120) -> tuple[int, str]:
    env = dict(os.environ)
    env["PATH"] = env.get("PATH", "") + ":/usr/local/bin:/opt/homebrew/bin:/Applications/Docker.app/Contents/Resources/bin"
    try:
        r = subprocess.run(["docker", "compose", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
        return r.returncode, (r.stdout + r.stderr).strip()
    except FileNotFoundError:
        return 127, "docker not found — is Docker Desktop installed and running?"
    except subprocess.TimeoutExpired:
        return 124, "docker compose timed out"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "user"


def list_gateways() -> list[dict]:
    doc = _load()
    changed = False
    # migrate: old ibgw (.env file at root) → profile "default"
    legacy_env = IBGW_ROOT / ".env"
    if not doc["gateways"] and legacy_env.exists() and read_env(legacy_env).get("TWS_USERID"):
        e = read_env(legacy_env)
        mode = e.get("TRADING_MODE", "paper")
        doc["gateways"].append({"id": "default", "name": f"{e['TWS_USERID']} · {mode}", "username": e["TWS_USERID"], "mode": mode,
                                "api_port": 4003 if mode == "live" else 4004, "vnc_port": 5901, "dir": str(IBGW_ROOT),
                                "created": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        changed = True
    if changed:
        _save(doc)
    return doc["gateways"]


def get(gid: str) -> dict | None:
    return next((g for g in list_gateways() if g["id"] == gid), None)


def upsert(username: str, password: str, mode: str) -> dict:
    """Create/update the profile for (user, mode); writes compose + .env"""
    gws = list_gateways()
    gid = f"{_slug(username)}-{mode}"
    g = next((x for x in gws if x["id"] == gid), None)
    if g is None:
        # legacy default with matching user/mode → reuse it
        g = next((x for x in gws if x["username"].lower() == username.lower() and x["mode"] == mode), None)
    if g is None:
        used_api = {x["api_port"] for x in gws} | {4003, 4004}
        used_vnc = {x["vnc_port"] for x in gws} | {5901}
        api_port = next(p for p in range(BASE_API_PORT, BASE_API_PORT + 200) if p not in used_api)
        vnc_port = next(p for p in range(BASE_VNC_PORT, BASE_VNC_PORT + 200) if p not in used_vnc)
        g = {"id": gid, "name": f"{username} · {mode}", "username": username, "mode": mode, "api_port": api_port, "vnc_port": vnc_port,
             "dir": str(IBGW_ROOT / "profiles" / gid), "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        gws.append(g)
    d = Path(g["dir"])
    d.mkdir(parents=True, exist_ok=True)
    if d != IBGW_ROOT:
        (d / "docker-compose.yml").write_text(COMPOSE_TMPL.format(project=f"quantx-gw-{gid}", image=IMAGE, api_port=g["api_port"],
                                                                  container_port=4003 if mode == "live" else 4004, vnc_port=g["vnc_port"]))
    env = {"TWS_USERID": username, "TWS_PASSWORD": password, "TRADING_MODE": mode}
    if not read_env(d / ".env").get("VNC_SERVER_PASSWORD"):
        env["VNC_SERVER_PASSWORD"] = secrets.token_urlsafe(12)      # VNC is bound to 127.0.0.1; a random password per gateway
    write_env(d / ".env", env)
    g["last_login"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _save({"gateways": gws})
    return g


def start(gid: str) -> tuple[int, str]:
    g = get(gid)
    if not g:
        return 404, "unknown gateway"
    return docker(["up", "-d", "--force-recreate"], Path(g["dir"]), timeout=180)


def stop(gid: str, forget_password: bool = True) -> tuple[int, str]:
    g = get(gid)
    if not g:
        return 404, "unknown gateway"
    if forget_password:
        write_env(Path(g["dir"]) / ".env", {"TWS_PASSWORD": ""})
    return docker(["down"], Path(g["dir"]))


def running(gid: str) -> bool:
    g = get(gid)
    if not g:
        return False
    code, out = docker(["ps", "--status", "running", "-q"], Path(g["dir"]), timeout=20)
    return code == 0 and bool(out.strip())


def remove(gid: str) -> None:
    stop(gid)
    doc = _load()
    doc["gateways"] = [g for g in doc["gateways"] if g["id"] != gid]
    _save(doc)
