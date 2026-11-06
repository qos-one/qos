"""
Detect that the machine slept / restarted / the API was stopped (step 15)

- Sleep while the API runs: time.monotonic stops while the machine sleeps (macOS / Linux) but the wall clock keeps going → the difference = sleep time
- API stopped / machine restarted: the last saved wall-clock time (data/wake.json) is too far from startup · boot time changed = machine restart
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def boot_time() -> float | None:
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=3).stdout
            m = re.search(r"sec = (\d+)", out)
            return float(m.group(1)) if m else None
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except Exception:
        return None
    return None


def uptime_clock() -> float:
    """Clock that stops while the machine sleeps: macOS CLOCK_UPTIME_RAW · Linux CLOCK_MONOTONIC (excludes suspend)"""
    if sys.platform == "darwin" and hasattr(time, "CLOCK_UPTIME_RAW"):
        return time.clock_gettime(time.CLOCK_UPTIME_RAW)
    return time.monotonic()


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


class WakeWatch:
    def __init__(self, state_file: Path, threshold_s: float = 120.0, wall=time.time, mono=uptime_clock, boot=boot_time):
        self.file, self.threshold = Path(state_file), threshold_s
        self.wall, self.mono, self.boot = wall, mono, boot
        self.last_wall: float | None = None
        self.last_mono: float | None = None
        self.events: list[dict] = []

    def _load(self) -> dict:
        try:
            return json.loads(self.file.read_text())
        except Exception:
            return {}

    def _save(self) -> None:
        doc = self._load()
        doc.update(last_wall=self.last_wall, boot=self.boot(), events=self.events[-20:] or doc.get("events", [])[-20:])
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, indent=1))
            tmp.replace(self.file)
        except OSError:
            pass

    def _event(self, kind: str, start: float, end: float) -> dict:
        ev = {"kind": kind, "from": _iso(start), "to": _iso(end), "minutes": round((end - start) / 60, 1)}
        self.events = (self._load().get("events") or [])[-19:] + [ev]
        return ev

    def startup(self) -> dict | None:
        """On API startup: the period the API was not running (reboot / api_down) if longer than the threshold"""
        doc = self._load()
        now, prev = self.wall(), doc.get("last_wall")
        self.last_wall, self.last_mono = now, self.mono()
        ev = None
        if prev and now - prev > self.threshold:
            b_now, b_prev = self.boot(), doc.get("boot")
            kind = "reboot" if b_now and b_prev and b_now - b_prev > 60 else "api_down"
            ev = self._event(kind, prev, now)
        self._save()
        return ev

    def tick(self) -> dict | None:
        """Called every ~15 seconds: machine slept between rounds = wall clock advanced more than monotonic"""
        now, mono = self.wall(), self.mono()
        ev = None
        if self.last_wall is not None and self.last_mono is not None:
            slept = (now - self.last_wall) - (mono - self.last_mono)
            if slept > self.threshold:
                ev = self._event("sleep", now - slept, now)
        self.last_wall, self.last_mono = now, mono
        self._save()
        return ev

    def recent(self) -> list[dict]:
        return (self._load().get("events") or [])[-10:]
