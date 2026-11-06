"""IBKR news and economic calendar (public ff_calendar_thisweek.json) with cache"""
from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta

import httpx

CAL_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_cal_cache: dict = {"ts": 0.0, "items": []}
_news_cache: dict[str, tuple[float, list[dict]]] = {}


async def calendar(force: bool = False) -> list[dict]:
    """This week's events: {title, country, date(iso utc), impact, forecast, previous, actual}"""
    if not force and time.time() - _cal_cache["ts"] < 1800 and _cal_cache["items"]:
        return _cal_cache["items"]
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "quantx-terminal/1.0"}) as c:
            r = await c.get(CAL_URL)
            r.raise_for_status()
            raw = r.json()
    except Exception as e:
        print("calendar fetch failed:", e)
        return _cal_cache["items"]
    items = []
    for ev in raw:
        try:
            dt = datetime.fromisoformat(str(ev.get("date")).replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            continue
        items.append({"title": ev.get("title", ""), "country": ev.get("country", ""), "date": dt.isoformat(timespec="minutes"),
                      "impact": str(ev.get("impact", "")).lower(), "forecast": ev.get("forecast", ""),
                      "previous": ev.get("previous", ""), "actual": ev.get("actual", "")})
    items.sort(key=lambda x: x["date"])
    _cal_cache.update(ts=time.time(), items=items)
    return items


def cache_age_s() -> float | None:
    """Seconds since the calendar was last fetched (None = never) — the risk loop must refresh it (see calendar())"""
    return time.time() - _cal_cache["ts"] if _cal_cache["ts"] else None


def upcoming_high_impact(items: list[dict], currencies: set[str], window_min: int) -> list[dict]:
    """High-impact events for the watched currencies within ±window minutes of now"""
    now = datetime.now(timezone.utc)
    out = []
    for ev in items:
        if ev["impact"] != "high" or (currencies and ev["country"].upper() not in currencies):
            continue
        dt = datetime.fromisoformat(ev["date"])
        if abs((dt - now).total_seconds()) <= window_min * 60:
            out.append(ev)
    return out


def currencies_for_key(key: str) -> set[str]:
    mkt, sym = key.split(":", 1)
    if mkt == "FX":
        return {sym[:3], sym[3:]}
    return {"USD"}


async def ibkr_news(ib, contract, limit: int = 20) -> list[dict]:
    key = f"{contract.conId}"
    ts, cached = _news_cache.get(key, (0.0, []))
    if time.time() - ts < 300 and cached:
        return cached
    out = []
    try:
        providers = await ib.reqNewsProvidersAsync()
        codes = "+".join(p.code for p in providers) or ""
        if codes:
            heads = await ib.reqHistoricalNewsAsync(contract.conId, codes, "", "", limit)
            for h in heads or []:
                out.append({"time": h.time.isoformat() if hasattr(h.time, "isoformat") else str(h.time),
                            "provider": h.providerCode, "article_id": h.articleId,
                            "headline": (h.headline or "").split("}", 1)[-1].strip()})
    except Exception as e:
        print("ibkr news:", e)
    _news_cache[key] = (time.time(), out)
    return out


async def ibkr_article(ib, provider: str, article_id: str) -> str:
    try:
        a = await ib.reqNewsArticleAsync(provider, article_id)
        return a.articleText or ""
    except Exception as e:
        return f"(article unavailable: {e})"
