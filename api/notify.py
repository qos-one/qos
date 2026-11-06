"""Telegram notifications (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env)"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from qos import paths  # noqa: E402  (step 17: all file locations come from QOS_HOME)

import httpx

CFG_FILE = paths.data_dir() / "notify.json"
DEFAULT = {"enabled": True, "events": {"order": True, "fill": True, "agent_stop": True, "error": True, "halt": True, "kill": True, "daily": True}}


def load() -> dict:
    d = json.loads(json.dumps(DEFAULT))
    try:
        saved = json.loads(CFG_FILE.read_text())
        d["enabled"] = saved.get("enabled", d["enabled"])
        d["events"].update(saved.get("events", {}))
    except Exception:
        pass
    return d


def save(cfg: dict) -> None:
    CFG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CFG_FILE.write_text(json.dumps(cfg, indent=2))


def configured() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))


async def send(text: str, event: str = "info") -> bool:
    cfg = load()
    if not cfg["enabled"] or not cfg["events"].get(event, True) or not configured():
        return False
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                             json={"chat_id": chat, "text": f"[quantX] {text}", "disable_web_page_preview": True})
            return r.status_code == 200
    except Exception as e:
        print("notify error:", e)
        return False


_loop: asyncio.AbstractEventLoop | None = None


def set_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    """The API's event loop — notifications fired from worker threads (asyncio.to_thread: node supervisor, /nodes/*) go through it"""
    global _loop
    _loop = loop


def _send_in_thread(text: str, event: str) -> None:
    try:
        asyncio.run(send(text, event))
    except Exception as e:
        print("notify error:", e)


def fire(text: str, event: str = "info") -> None:
    """Fire-and-forget send (for use from sync code, on the event loop or from a worker thread)"""
    global _loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        _loop = _loop or loop
        loop.create_task(send(text, event))
        return
    # worker thread: hand over to the API loop if it is running, otherwise send from a short-lived daemon thread
    main = _loop
    if main is not None and main.is_running() and not main.is_closed():
        try:
            asyncio.run_coroutine_threadsafe(send(text, event), main)
            return
        except RuntimeError:
            pass
    threading.Thread(target=_send_in_thread, args=(text, event), name="qos-notify", daemon=True).start()
