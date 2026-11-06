"""
Currency conversion to USD — one implementation shared by the API, nodes, backtests and parity.

Pairs can be written "EUR/USD", "EURUSD" or "EUR/USD.IDEALPRO". `rates` maps a currency to the USD value of one
unit of it, e.g. {"EUR": 1.08, "JPY": 0.0066, "USD": 1.0}. Every function returns None when it cannot price a
value; callers that guard money (risk limits) must treat None as "block", never as zero.
"""
from __future__ import annotations

import math

Rates = dict[str, float]


def split_pair(sym: str) -> tuple[str, str]:
    """'EUR/USD.IDEALPRO' | 'EUR/USD' | 'EURUSD' | 'FX:EURUSD' → ('EUR', 'USD') · non-FX symbols → (sym, 'USD')"""
    s = sym.split(":")[-1].split(".")[0].upper()
    if "/" in s:
        base, _, quote = s.partition("/")
        return base, quote or "USD"
    if len(s) == 6 and s.isalpha():
        return s[:3], s[3:]
    return s, "USD"


def _ok(x) -> bool:
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x) and x > 0


def usd_rate(ccy: str, rates: Rates | None = None) -> float | None:
    """USD value of one unit of `ccy`."""
    ccy = ccy.upper()
    if ccy == "USD":
        return 1.0
    r = (rates or {}).get(ccy)
    return float(r) if _ok(r) else None


def quote_to_usd(sym: str, price: float | None, rates: Rates | None = None) -> float | None:
    """USD value of one unit of the pair's quote currency (multiply quote-currency P&L by this)."""
    base, quote = split_pair(sym)
    if quote == "USD":
        return 1.0
    if base == "USD":
        return 1.0 / price if _ok(price) else usd_rate(quote, rates)
    return usd_rate(quote, rates)


def base_to_usd(sym: str, price: float | None, rates: Rates | None = None) -> float | None:
    """USD value of one unit of the pair's base currency (or one share / coin for non-FX)."""
    base, quote = split_pair(sym)
    if base == "USD":
        return 1.0
    if quote == "USD":
        return float(price) if _ok(price) else usd_rate(base, rates)
    direct = usd_rate(base, rates)
    if direct is not None:
        return direct
    q = usd_rate(quote, rates)
    return price * q if (q is not None and _ok(price)) else None


def notional_usd(sym: str, qty: float, price: float | None, rates: Rates | None = None) -> float | None:
    """|qty| units of the pair's base currency (or shares / coins) valued in USD; None if it cannot be priced."""
    if qty is None or not math.isfinite(qty):
        return None
    b = base_to_usd(sym, price, rates)
    return abs(qty) * b if b is not None else None


def pnl_to_usd(sym: str, amount_quote: float, price: float | None, rates: Rates | None = None) -> float | None:
    """An amount in the pair's quote currency (P&L, commission) converted to USD."""
    q = quote_to_usd(sym, price, rates)
    return amount_quote * q if q is not None else None


def rates_from_prices(prices: dict[str, float], base: Rates | None = None) -> Rates:
    """{'EURUSD': 1.08, 'USDJPY': 151.0, …} → {'EUR': 1.08, 'JPY': 0.00662, 'USD': 1.0} (only pairs against USD)."""
    out: Rates = {"USD": 1.0, **(base or {})}
    for pair, px in prices.items():
        if not _ok(px):
            continue
        b, q = split_pair(pair)
        if q == "USD" and b != "USD":
            out[b] = float(px)
        elif b == "USD" and q != "USD":
            out[q] = 1.0 / float(px)
    return out
