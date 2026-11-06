"""
Keep the machine awake while nodes are running (step 15)

macOS: hold a power assertion with `caffeinate -i -s` (prevents idle sleep · -s applies on AC power) while any node runs, released when none
Closing the laptop lid / manual Sleep / restart for updates — the assertion cannot prevent these → WakeWatch (api/wake.py) detects them and alerts
Linux / others: not supported (reported in /system/health)
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time


def pmset_ac() -> dict:
    """Power settings on AC (pmset -g custom) · will_sleep = the machine can sleep on its own (sleep > 0)"""
    if sys.platform != "darwin" or not shutil.which("pmset"):
        return {"supported": False}
    try:
        out = subprocess.run(["pmset", "-g", "custom"], capture_output=True, text=True, timeout=5).stdout
    except Exception as e:
        return {"supported": False, "error": str(e)[:120]}
    return parse_pmset(out)


def parse_pmset(out: str) -> dict:
    sect, ac = None, {}
    for line in out.splitlines():
        if line.strip().endswith("Power:"):
            sect = line.strip().rstrip(":")
            continue
        m = re.match(r"^\s+(\w+)\s+(\S+)", line)
        if m and sect == "AC Power":
            ac[m.group(1)] = m.group(2)
    sleep = int(ac.get("sleep", "0")) if ac.get("sleep", "0").isdigit() else 0
    return {"supported": True, "ac": ac, "sleep_min": sleep, "will_sleep": sleep > 0,
            "standby": ac.get("standby") == "1", "hint": "sudo pmset -c sleep 0 disksleep 0 standby 0 autopoweroff 0" if sleep > 0 else ""}


class PowerGuard:
    def __init__(self, platform: str | None = None, spawn=subprocess.Popen):
        self.platform = platform or sys.platform
        self.spawn = spawn
        self.proc = None
        self.since: float | None = None

    @property
    def supported(self) -> bool:
        return self.platform == "darwin" and bool(shutil.which("caffeinate") or self.spawn is not subprocess.Popen)

    def active(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def update(self, want: bool) -> bool:
        """want = a node is running · returns the assertion state after adjusting"""
        if not self.supported:
            return False
        if want and not self.active():
            self.proc = self.spawn(["caffeinate", "-i", "-s"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.since = time.time()
        elif not want and self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                pass
            self.proc, self.since = None, None
        return self.active()

    def status(self) -> dict:
        return {"supported": self.supported, "active": self.active(), "since": self.since,
                "pid": self.proc.pid if self.active() else None, "pmset": pmset_ac()}
