"""
QOS watchdog (step 15) — run by launchd every 60 seconds, separate from the API (standard library only)

  api      /health fails 3 times in a row → launchctl kickstart -k <api label> (macOS) or systemctl --user restart <api unit> (Linux) · still failing 3 more times → Telegram (sent directly, not via the API)
  redis    port unreachable for 3 minutes → Telegram
  gateway  selected IB Gateway port unreachable for 10 minutes → Telegram (the gateway's nightly self-restart takes only a few minutes)
  one alert per incident, plus one on recovery · latest status is written to data/watchdog.json (Terminal › System)

  python scripts/watchdog.py --api-label com.qos-api [--port 8000] [--once]
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from qos import paths  # noqa: E402

THRESHOLDS = {"api": 3, "redis": 3, "gateway": 10}         # consecutive rounds (1 round = 1 minute)
API_ALERT_AFTER = 6                                        # kickstart at round 3 · still down at round 6 = alert


def read_env() -> dict:
    out = {}
    try:
        for line in paths.env_file().read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return {**out, **{k: v for k, v in os.environ.items() if k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "REDIS_URL", "IB_PORT")}}


def http_ok(url: str, timeout: float = 5.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def tcp_ok(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def gateway_port(env: dict | None = None) -> int | None:
    """API port of the gateway the node uses — same order as nautilus_bridge.resolve_gateway: IB_PORT > selected profile > first profile
    IB_PORT counts from the process env or from .env (the API loads .env at startup; launchd gives the watchdog neither)"""
    env = read_env() if env is None else env
    port = os.getenv("IB_PORT") or env.get("IB_PORT")
    if port:
        try:
            return int(port)
        except ValueError:
            pass
    try:
        sess = json.loads((paths.data_dir() / "session.json").read_text())
    except Exception:
        sess = {}
    try:
        gws = json.loads((paths.data_dir() / "gateways.json").read_text()).get("gateways", [])
    except Exception:
        gws = []
    g = next((x for x in gws if x.get("id") == sess.get("gateway_id")), None) or (gws[0] if gws else None)
    return int(g["api_port"]) if g and g.get("api_port") else None


def telegram(env: dict, text: str) -> bool:
    tok, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": f"QOS watchdog: {text}"}).encode()
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data=data, timeout=10) as r:
            return r.status == 200
    except Exception:
        return False


def step(state: dict, results: dict[str, bool | None], now: float) -> list[tuple[str, str]]:
    """Update counters → list of actions: ("kickstart", "") · ("alert", message) — pure logic (testable)"""
    actions = []
    for name, ok in results.items():
        s = state.setdefault(name, {"fails": 0, "alerted": False, "since": None})
        if ok is None:                                     # not checked (e.g. no gateway selected yet)
            continue
        if ok:
            if s["alerted"]:
                actions.append(("alert", f"{name} is back (down {round((now - (s['since'] or now)) / 60)} min)"))
            s.update(fails=0, alerted=False, since=None)
            continue
        s["fails"] += 1
        s["since"] = s["since"] or now
        if name == "api":
            if s["fails"] == THRESHOLDS["api"]:
                actions.append(("kickstart", ""))
            if s["fails"] >= API_ALERT_AFTER and not s["alerted"]:
                s["alerted"] = True
                actions.append(("alert", f"QOS API is not responding for {s['fails']} min — restarted it, still down. Nodes keep running on their own."))
        elif s["fails"] >= THRESHOLDS.get(name, 3) and not s["alerted"]:
            s["alerted"] = True
            what = {"redis": "Redis/Valkey is down — node events and kill switch messages cannot reach the nodes",
                    "gateway": "IB Gateway port is not reachable — nodes cannot trade"}.get(name, f"{name} is down")
            actions.append(("alert", f"{what} ({s['fails']} min)"))
    return actions


def run_once(api_label: str, port: int, api_unit: str = "") -> dict:
    env = read_env()
    sf = paths.data_dir() / "watchdog.json"
    try:
        state = json.loads(sf.read_text()).get("state", {})
    except Exception:
        state = {}
    redis = urllib.parse.urlparse(env.get("REDIS_URL") or "redis://127.0.0.1:6379/0")
    gw = gateway_port(env)
    results = {"api": http_ok(f"http://127.0.0.1:{port}/health"),
               "redis": tcp_ok(redis.hostname or "127.0.0.1", redis.port or 6379),
               "gateway": tcp_ok("127.0.0.1", gw) if gw else None}
    now = time.time()
    done = []
    for kind, text in step(state, results, now):
        if kind == "kickstart" and api_unit:                    # Linux server: systemd user unit (deploy/systemd)
            r = subprocess.run(["systemctl", "--user", "restart", api_unit], capture_output=True, text=True)
            done.append(f"restart {api_unit}: {'ok' if r.returncode == 0 else r.stderr.strip()[:120]}")
        elif kind == "kickstart" and api_label:
            r = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{api_label}"], capture_output=True, text=True)
            done.append(f"kickstart {api_label}: {'ok' if r.returncode == 0 else r.stderr.strip()[:120]}")
        elif kind == "alert":
            done.append(("sent: " if telegram(env, text) else "not sent (no Telegram in .env): ") + text)
    doc = {"ts": now, "checks": results, "gateway_port": gw, "state": state, "actions": done}
    try:
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text(json.dumps(doc, indent=1))
    except OSError:
        pass
    for line in done:
        print(time.strftime("%Y-%m-%d %H:%M:%S"), line, flush=True)
    return doc


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api-label", default=os.getenv("QOS_API_LABEL", ""))
    ap.add_argument("--api-unit", default=os.getenv("QOS_API_UNIT", ""), help="systemd user unit to restart instead of launchctl (Linux)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--once", action="store_true", help="(default under launchd) check once and exit")
    a = ap.parse_args()
    run_once(a.api_label, a.port, a.api_unit)


if __name__ == "__main__":
    main()
