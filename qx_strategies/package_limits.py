"""
Per-strategy-package limits (step 22) — enforced by QOS, not by the strategy code

Layer 1 (node): QXBaseStrategy._enter calls LIMITS.check(record=False) before every order that opens/adds to a position and
               LIMITS.commit() once every later check (portfolio) passed — blocked entries are not counted
Layer 2 (API): api/package_guard.py checks the node's order/fill events against the same limits

  instruments          orders allowed only for instruments declared in the manifest
  max_notional_usd     position value after the order (USD) per instrument
  max_orders_per_hour  open/add orders per hour (closing/reducing is never blocked)
  max_flips_per_hour   long ↔ short flips per hour

Over a limit = order rejected + risk event "package_limit" · 3 breaches within 10 minutes = tripped (all new entries blocked and the API stops the node)
Does not import nautilus — uses event time (ts_ns), so it behaves the same in backtest and live
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from . import fx

HOUR_NS = 3600 * 1_000_000_000
TRIP_WINDOW_NS = 600 * 1_000_000_000
TRIP_COUNT = 3

DEFAULTS = {"max_notional_usd": 500_000.0, "max_orders_per_hour": 60, "max_flips_per_hour": 30, "max_memory_mb": 2048}
KEYS = ("max_notional_usd", "max_orders_per_hour", "max_flips_per_hour", "max_memory_mb")


def resolve(manifest: dict | None, instrument: str | None = None) -> dict:
    """Effective limits: manifest values (package) or defaults · instruments = declared ones (builtin = the config's instrument)"""
    m = manifest or {}
    src = {**(m.get("limits") or {})}
    out = {k: src.get(k) if src.get(k) not in (None, "") else DEFAULTS[k] for k in KEYS}
    out["instruments"] = list(m.get("instruments") or ([instrument] if instrument else []))
    return out


def usd_notional(instrument: str, qty: float, price: float, rates: dict | None = None) -> float | None:
    """USD value of |qty| (crosses such as EUR/GBP, EUR/JPY via the USD rates) · None = cannot be priced"""
    from .risk import fx_symbol
    return fx.notional_usd(fx_symbol(instrument), qty, price, rates)


@dataclass
class PackageLimits:
    limits: dict | None = None
    orders: deque = field(default_factory=deque)          # ts_ns of open/add orders
    flips: deque = field(default_factory=deque)
    breaches: deque = field(default_factory=deque)
    tripped: bool = False
    stats: dict = field(default_factory=lambda: {"max_orders_per_hour": 0, "max_flips_per_hour": 0, "max_notional_usd": 0.0, "breaches": 0})

    def configure(self, limits: dict | None) -> None:
        self.__init__()
        self.limits = dict(limits) if limits else None

    def reset(self) -> None:
        self.configure(None)

    @staticmethod
    def _trim(q: deque, now: int, window: int) -> None:
        while q and now - q[0] >= window:
            q.popleft()

    @staticmethod
    def is_flip(side: int, net_qty: float) -> bool:
        return net_qty != 0 and (net_qty > 0) != (side > 0)

    def check(self, instrument: str, side: int, qty: float, price: float, net_qty: float, ts_ns: int,
              rates: dict | None = None, record: bool = True) -> tuple[bool, str, str]:
        """Before an open/add order: side +1 buy / −1 sell · net_qty = current position (+/−) → (ok?, reason, limit name)
        rates = USD rates from the API (live) · None = no rate source (backtest): crosses that cannot be priced skip the notional cap
        record=False → the caller runs further checks first and calls commit() only when the order is actually sent"""
        L = self.limits
        flip = self.is_flip(side, net_qty)
        exposure = (0.0 if flip else abs(net_qty)) + abs(qty)
        notional = usd_notional(instrument, exposure, price, rates)
        self._trim(self.orders, ts_ns, HOUR_NS)
        self._trim(self.flips, ts_ns, HOUR_NS)
        st = self.stats
        if notional is not None:
            st["max_notional_usd"] = max(st["max_notional_usd"], notional)
        if L is None:
            if record:
                self._record(ts_ns, flip)
            return True, "", ""
        if self.tripped:
            return False, "package limits breached 3× in 10 min — new entries blocked until the node restarts", "tripped"
        allowed = L.get("instruments") or []
        if allowed and instrument not in allowed:
            return self._breach(ts_ns, f"{instrument} is not in the package's instruments ({', '.join(allowed)})", "instruments")
        cap = float(L.get("max_notional_usd") or 0)
        if cap > 0 and notional is None and rates is not None:          # live: unknown FX rate = block (not a package breach — missing data)
            return False, f"cannot value {instrument} in USD (no FX rate) — order blocked", "rate"
        if cap > 0 and notional is not None and notional > cap:
            return self._breach(ts_ns, f"position would be {notional:,.0f} USD > max_notional_usd {cap:,.0f}", "max_notional_usd")
        cap = int(L.get("max_orders_per_hour") or 0)
        if cap > 0 and len(self.orders) >= cap:
            return self._breach(ts_ns, f"{len(self.orders)} entry orders in the last hour ≥ max_orders_per_hour {cap}", "max_orders_per_hour")
        cap = L.get("max_flips_per_hour")
        if flip and cap is not None and len(self.flips) >= int(cap):            # 0 = no flips allowed
            return self._breach(ts_ns, f"{len(self.flips)} side flips in the last hour ≥ max_flips_per_hour {cap}", "max_flips_per_hour")
        if record:
            self._record(ts_ns, flip)
        return True, "", ""

    def commit(self, side: int, net_qty: float, ts_ns: int) -> None:
        """Count an entry that passed check(record=False) and every later check — called right before the order is submitted"""
        self._trim(self.orders, ts_ns, HOUR_NS)
        self._trim(self.flips, ts_ns, HOUR_NS)
        self._record(ts_ns, self.is_flip(side, net_qty))

    def _record(self, ts_ns: int, flip: bool) -> None:
        self.orders.append(ts_ns)
        if flip:
            self.flips.append(ts_ns)
        st = self.stats
        st["max_orders_per_hour"] = max(st["max_orders_per_hour"], len(self.orders))
        st["max_flips_per_hour"] = max(st["max_flips_per_hour"], len(self.flips))

    def _breach(self, ts_ns: int, why: str, kind: str) -> tuple[bool, str, str]:
        while self.breaches and ts_ns - self.breaches[0] > TRIP_WINDOW_NS:       # 10-minute edge inclusive (5-minute bars: t, t+5, t+10 = 3 breaches)
            self.breaches.popleft()
        self.breaches.append(ts_ns)
        self.stats["breaches"] += 1
        if len(self.breaches) >= TRIP_COUNT:
            self.tripped = True
        return False, why, kind

    def summary(self) -> dict:
        return {"limits": self.limits, "tripped": self.tripped, "orders_last_hour": len(self.orders), "flips_last_hour": len(self.flips),
                "breaches_10m": len(self.breaches)}


LIMITS = PackageLimits()
