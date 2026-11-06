"""
Portfolio-wide risk (step 16) — computed per "currency" (USD base account)

- exposure per currency = that currency's cash balance (not IBKR's virtual positions) converted to USD · USD = −sum of the others
  long EUR/USD 20,000 → EUR +21,600 USD · USD −21,600 · long EUR/USD + GBP/USD + AUD/USD → USD short across all 3 pairs
- correlated groups: daily returns of each currency vs USD (lookback days) |corr| ≥ threshold → same group
  group exposure = |Σ direction × exposure| (direction relative to the first member)
- 1-day 95% VaR: historical simulation — P&L_t = Σ exposure_ccy × return_ccy,t · VaR = −5th percentile
- order check: fails only if after the order it is "over the limit AND worse than before" → risk-reducing orders always pass
Used by both the API (overview) and the node (pre-order check) — does not import nautilus / IB
"""
from __future__ import annotations

import math

from . import fx

DEFAULT_LIMITS = {"max_gross_x": 5.0, "max_currency_pct": 150.0, "max_cluster_pct": 200.0, "max_var_pct": 1.5,
                  "max_dd_pct": 5.0, "min_excess_liq_pct": 30.0, "alert_pct": 80.0, "corr_threshold": 0.7, "lookback_days": 60}
METRICS = ("gross", "currency", "cluster", "var")


def clean_limits(d: dict | None) -> dict:
    out = {**DEFAULT_LIMITS, **{k: float(v) for k, v in (d or {}).items() if k in DEFAULT_LIMITS and v is not None}}
    bad = sorted(k for k, v in out.items() if not math.isfinite(v))
    if bad:                                                   # NaN / inf would make every "value > limit" comparison False = limit disabled
        raise ValueError(f"{', '.join(bad)} must be a finite number")
    for k in ("max_gross_x", "max_currency_pct", "max_cluster_pct", "max_var_pct", "max_dd_pct"):
        if out[k] <= 0:
            raise ValueError(f"{k} must be > 0")
    if not 0 <= out["min_excess_liq_pct"] < 100 or not 0 < out["alert_pct"] <= 100 or not 0 < out["corr_threshold"] <= 1:
        raise ValueError("min_excess_liq_pct 0–99, alert_pct 1–100, corr_threshold 0–1")
    out["lookback_days"] = int(max(20, min(500, out["lookback_days"])))
    return out


def limits_usd(limits: dict, netliq: float) -> dict:
    nl = max(float(netliq or 0), 0.0)
    return {"gross": limits["max_gross_x"] * nl, "currency": limits["max_currency_pct"] / 100 * nl,
            "cluster": limits["max_cluster_pct"] / 100 * nl, "var": limits["max_var_pct"] / 100 * nl}


def pair_parts(inst: str) -> tuple[str, str]:
    """'EUR/USD.IDEALPRO' · 'FX:EURUSD' · 'EURUSD' · 'EUR/USD' → ('EUR', 'USD')"""
    s = inst.split(":")[-1].split(".")[0].replace("/", "").upper()
    return s[:3], s[3:6]


def with_usd(exp: dict) -> dict:
    out = {k: v for k, v in exp.items() if k != "USD"}
    out["USD"] = -sum(out.values())
    return out


def _finite_pos(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x) and x > 0


def order_delta(inst: str, side: int, qty: float, price: float, rates: dict) -> dict | None:
    """Effect of an order on non-USD currency exposure (USD) · side +1 buy / −1 sell · qty in base-currency units
    None = a leg cannot be valued in USD (no rate) — check_order blocks instead of treating it as zero"""
    base, quote = pair_parts(inst)
    d: dict = {}
    if base != "USD":
        r = fx.usd_rate(base, rates)
        if r is None and quote == "USD" and _finite_pos(price):
            r = float(price)                                   # XXX/USD: the order price is the base rate
        if r is None:
            return None
        d[base] = d.get(base, 0.0) + side * qty * r
    if quote != "USD":
        r = fx.usd_rate(quote, rates)
        if r is None and base == "USD" and _finite_pos(price):
            r = 1.0 / float(price)                             # USD/XXX: 1 / price is the quote rate
        if r is None or not _finite_pos(price):
            return None
        d[quote] = d.get(quote, 0.0) - side * qty * price * r
    return d


def gross(exp: dict) -> float:
    """Sum of |exposure| of non-USD currencies (≈ total notional of all pairs vs USD)"""
    return sum(abs(v) for k, v in exp.items() if k != "USD")


def max_currency(exp: dict) -> tuple[str, float]:
    full = with_usd(exp)
    if not full:
        return "", 0.0
    k = max(full, key=lambda c: abs(full[c]))
    return k, abs(full[k])


def cluster_exposures(exp: dict, clusters: list[list[str]], signs: dict) -> list[dict]:
    out = []
    for members in clusters:
        v = sum(signs.get(c, 1) * exp.get(c, 0.0) for c in members)
        out.append({"members": members, "exposure": abs(v)})
    return out


def var95(exp: dict, returns: dict) -> float | None:
    cols = [c for c in exp if c != "USD" and abs(exp[c]) > 0]
    if not cols:
        return 0.0
    if any(c not in returns or not returns[c] for c in cols):
        return None
    n = min(len(returns[c]) for c in cols)
    pnl = sorted(sum(exp[c] * returns[c][-n + t] for c in cols) for t in range(n))
    return round(max(0.0, -pnl[int(math.floor(0.05 * (n - 1)))]), 2)


def metrics(exp: dict, stats: dict) -> dict:
    cl = cluster_exposures(exp, stats.get("clusters") or [], stats.get("signs") or {})
    ccy, cv = max_currency(exp)
    return {"gross": round(gross(exp), 2), "currency": round(cv, 2), "currency_max": ccy,
            "cluster": round(max((c["exposure"] for c in cl), default=0.0), 2), "clusters": cl,
            "var": var95(exp, stats.get("returns") or {})}


def check_order(p: dict | None, inst: str, side: int, qty: float, price: float, net_qty: float = 0.0) -> tuple[bool, str, str]:
    """p = payload['portfolio'] from the API · net_qty = this strategy's position (flip = close the old side first)"""
    if not p or not p.get("limits_usd"):
        return True, "", ""
    rates, exp = p.get("rates") or {}, dict(p.get("exposure") or {})
    flip = net_qty != 0 and (net_qty > 0) != (side > 0)
    d = order_delta(inst, side, qty, price, rates)
    if d is None:
        return False, f"cannot value {inst} in USD for portfolio limits (no FX rate) — order blocked", "rate"
    if flip:                                                  # the closing order was already sent, but account balances have not changed yet
        dc = order_delta(inst, -1 if net_qty > 0 else 1, abs(net_qty), price, rates) or {}
        for k, v in dc.items():
            d[k] = d.get(k, 0.0) + v
    after = dict(exp)
    for k, v in d.items():
        after[k] = after.get(k, 0.0) + v
    stats = {"clusters": p.get("clusters"), "signs": p.get("signs"), "returns": p.get("returns")}
    mb, ma, lim = metrics(exp, stats), metrics(after, stats), p["limits_usd"]
    for m in METRICS:
        a, b = ma.get(m), mb.get(m)
        if a is None or not lim.get(m):
            continue
        if a > lim[m] and a > (b or 0) + 1e-6:
            what = {"gross": "gross FX exposure", "currency": f"{ma['currency_max']} exposure", "cluster": "correlated group exposure",
                    "var": "1-day VaR (95%)"}[m]
            return False, f"{what} would be {a:,.0f} USD > portfolio limit {lim[m]:,.0f}", m
    return True, "", ""


# ---------- Statistics from historical data (computed by the API once a day) ----------
def currency_returns(closes: dict[str, list[tuple[str, float]]]) -> tuple[list[str], dict]:
    """closes: {'EURUSD': [(date, close), …]} → (dates common to all pairs, {currency: daily return vs USD})"""
    by = {}
    for pair, rows in closes.items():
        base, quote = pair_parts(pair)
        if "USD" not in (base, quote) or base == quote:
            continue
        ccy, inv = (base, False) if quote == "USD" else (quote, True)
        by[ccy] = ({d: c for d, c in rows if c > 0}, inv)
    if not by:
        return [], {}
    dates = sorted(set.intersection(*(set(v[0]) for v in by.values())))
    rets = {}
    for ccy, (m, inv) in by.items():
        r = []
        for a, b in zip(dates[:-1], dates[1:]):
            x = m[b] / m[a] - 1
            r.append(1 / (1 + x) - 1 if inv else x)
        rets[ccy] = r
    return dates[1:], rets


def corr(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 3:
        return 0.0
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return 0.0
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(va * vb)


def clusters_from(rets: dict, threshold: float) -> tuple[list[list[str]], dict, dict]:
    ccys = sorted(rets)
    cm = {f"{a}/{b}": round(corr(rets[a], rets[b]), 3) for i, a in enumerate(ccys) for b in ccys[i + 1:]}
    parent = {c: c for c in ccys}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for k, v in cm.items():
        if abs(v) >= threshold:
            a, b = k.split("/")
            parent[find(a)] = find(b)
    groups: dict = {}
    for c in ccys:
        groups.setdefault(find(c), []).append(c)
    clusters = [sorted(g) for g in groups.values() if len(g) > 1]
    signs = {}
    for g in clusters:
        ref = g[0]
        signs[ref] = 1
        for c in g[1:]:
            k = f"{min(ref, c)}/{max(ref, c)}"
            signs[c] = 1 if cm.get(k, 0) >= 0 else -1
    return clusters, signs, cm
