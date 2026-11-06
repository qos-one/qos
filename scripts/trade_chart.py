"""
Fetch executions + 1-minute prices from IB and build an HTML chart

Usage: uv run scripts/trade_chart.py
Open reports/trade_chart.html in a browser
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from ib_async import IB, ExecutionFilter, Forex

HOST = "127.0.0.1"
PORT = 4004
CLIENT_ID = 76
ICT = ZoneInfo("Asia/Bangkok")
OUT = Path(__file__).resolve().parent.parent / "reports" / "trade_chart.html"


def to_ict(ts) -> datetime:
    if isinstance(ts, datetime):
        dt = ts
    else:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ICT)


def pair_rounds(fills: list[dict]) -> list[dict]:
    open_long: list[dict] = []
    rounds: list[dict] = []
    for f in fills:
        if f["side"] == "BOT":
            open_long.append(f)
            continue
        if f["side"] == "SLD" and open_long:
            buy = open_long.pop(0)
            qty = min(buy["shares"], f["shares"])
            gross = (f["price"] - buy["price"]) * qty
            comm = (buy["commission"] or 0) + (f["commission"] or 0)
            hold = (f["dt"] - buy["dt"]).total_seconds()
            rounds.append(
                {
                    "symbol": buy["symbol"],
                    "qty": qty,
                    "buy": buy,
                    "sell": f,
                    "gross": gross,
                    "commission": comm,
                    "net": gross - comm,
                    "hold_sec": hold,
                }
            )
    return rounds


def svg_price(bars: list[dict], fills: list[dict]) -> str:
    if not bars:
        return "<p class='muted'>No price bars</p>"
    W, H = 960, 340
    pad = {"l": 64, "r": 20, "t": 24, "b": 44}
    plot_w = W - pad["l"] - pad["r"]
    plot_h = H - pad["t"] - pad["b"]
    t0 = bars[0]["dt"]
    t1 = bars[-1]["dt"]
    span = max((t1 - t0).total_seconds(), 1)
    prices = [b["close"] for b in bars] + [f["price"] for f in fills]
    y_min, y_max = min(prices), max(prices)
    pad_y = (y_max - y_min) * 0.12 or 0.0001
    y_min -= pad_y
    y_max += pad_y

    def x_at(dt: datetime) -> float:
        return pad["l"] + ((dt - t0).total_seconds() / span) * plot_w

    def y_at(p: float) -> float:
        return pad["t"] + ((y_max - p) / (y_max - y_min)) * plot_h

    pts = " ".join(f"{x_at(b['dt']):.1f},{y_at(b['close']):.1f}" for b in bars)
    ticks = [y_min, (y_min + y_max) / 2, y_max]
    y_grid = []
    for p in ticks:
        y = y_at(p)
        y_grid.append(
            f'<line x1="{pad["l"]}" x2="{W-pad["r"]}" y1="{y:.1f}" y2="{y:.1f}" />'
            f'<text x="{pad["l"]-8}" y="{y+4:.1f}" text-anchor="end" class="axis">'
            f"{p:.5f}</text>"
        )
    n = len(bars)
    step = max(1, n // 7)
    x_labs = []
    for i, b in enumerate(bars):
        if i % step and i != n - 1:
            continue
        x = x_at(b["dt"])
        x_labs.append(
            f'<text x="{x:.1f}" y="{H-14}" text-anchor="middle" class="axis">'
            f"{b['dt'].strftime('%H:%M')}</text>"
        )
    marks = []
    for f in fills:
        x, y = x_at(f["dt"]), y_at(f["price"])
        color = "#3dd68c" if f["side"] == "BOT" else "#ff6b6b"
        if f["side"] == "BOT":
            d = f"M {x:.1f} {y-8:.1f} L {x-6.5:.1f} {y+5:.1f} L {x+6.5:.1f} {y+5:.1f} Z"
        else:
            d = f"M {x:.1f} {y+8:.1f} L {x-6.5:.1f} {y-5:.1f} L {x+6.5:.1f} {y-5:.1f} Z"
        marks.append(
            f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{pad["t"]}" y2="{pad["t"]+plot_h}" '
            f'class="mark-line" stroke="{color}" />'
            f'<path d="{d}" fill="{color}" />'
        )
    return f"""
    <svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="Price and fills">
      <g class="grid">{''.join(y_grid)}</g>
      <polyline points="{pts}" fill="none" stroke="#6cb6ff" stroke-width="2"
        stroke-linejoin="round" stroke-linecap="round" />
      {''.join(marks)}
      {''.join(x_labs)}
    </svg>
    """


def svg_bars(values: list[float], labels: list[str], title: str) -> str:
    W, H = 460, 220
    pad = {"l": 48, "r": 16, "t": 28, "b": 36}
    plot_w = W - pad["l"] - pad["r"]
    plot_h = H - pad["t"] - pad["b"]
    lo = min(0, min(values, default=0))
    hi = max(0, max(values, default=0))
    if lo == hi:
        hi = 1
    span = hi - lo
    n = max(len(values), 1)
    gap = 16
    bw = (plot_w - gap * (n + 1)) / n
    zero_y = pad["t"] + ((hi - 0) / span) * plot_h
    rects = []
    for i, v in enumerate(values):
        x = pad["l"] + gap + i * (bw + gap)
        yv = pad["t"] + ((hi - v) / span) * plot_h
        y = min(yv, zero_y)
        h = abs(yv - zero_y)
        color = "#3dd68c" if v >= 0 else "#ff6b6b"
        rects.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{max(h, 2):.1f}" rx="3" fill="{color}" />'
            f'<text x="{x+bw/2:.1f}" y="{H-12}" text-anchor="middle" class="axis">{escape(labels[i])}</text>'
            f'<text x="{x+bw/2:.1f}" y="{y-6:.1f}" text-anchor="middle" class="val">{v:+.2f}</text>'
        )
    return f"""
    <svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="{escape(title)}">
      <text x="{pad['l']}" y="16" class="svg-title">{escape(title)}</text>
      <line x1="{pad['l']}" x2="{W-pad['r']}" y1="{zero_y:.1f}" y2="{zero_y:.1f}" class="zero" />
      {''.join(rects)}
    </svg>
    """


def svg_equity(gross_pts: list[float], net_pts: list[float], labels: list[str]) -> str:
    W, H = 460, 220
    pad = {"l": 48, "r": 16, "t": 28, "b": 36}
    plot_w = W - pad["l"] - pad["r"]
    plot_h = H - pad["t"] - pad["b"]
    vals = gross_pts + net_pts
    lo, hi = min(vals), max(vals)
    pad_y = (hi - lo) * 0.15 or 1
    lo -= pad_y
    hi += pad_y
    span = hi - lo

    def xy(i: int, v: float) -> tuple[float, float]:
        x = pad["l"] + (i / max(len(labels) - 1, 1)) * plot_w
        y = pad["t"] + ((hi - v) / span) * plot_h
        return x, y

    def poly(series: list[float]) -> str:
        return " ".join(f"{xy(i, v)[0]:.1f},{xy(i, v)[1]:.1f}" for i, v in enumerate(series))

    labs = []
    for i, lab in enumerate(labels):
        x, _ = xy(i, 0)
        labs.append(
            f'<text x="{x:.1f}" y="{H-12}" text-anchor="middle" class="axis">{escape(lab)}</text>'
        )
    return f"""
    <svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="Cumulative PnL">
      <text x="{pad['l']}" y="16" class="svg-title">Cumulative PnL (USD)</text>
      <polyline points="{poly(gross_pts)}" fill="none" stroke="#e8c030" stroke-width="2" />
      <polyline points="{poly(net_pts)}" fill="none" stroke="#ff6b6b" stroke-width="2" />
      {''.join(labs)}
    </svg>
    """


HTML = """<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Trade chart</title>
<style>
  :root {{
    --bg: #0e1116;
    --panel: #161b22;
    --line: #2a3340;
    --text: #e8edf4;
    --muted: #8b98a8;
    --buy: #3dd68c;
    --sell: #ff6b6b;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
    background: var(--bg);
    color: var(--text);
    padding: 32px 28px 48px;
  }}
  h1 {{ font-size: 22px; font-weight: 600; margin: 0 0 6px; }}
  .sub {{ color: var(--muted); font-size: 13px; margin-bottom: 24px; }}
  .stats {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 20px; }}
  .stat {{ background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; }}
  .stat .v {{ font-size: 22px; font-weight: 600; letter-spacing: -0.03em; }}
  .stat .l {{ color: var(--muted); font-size: 12px; margin-top: 4px; }}
  .neg {{ color: var(--sell); }}
  .pos {{ color: var(--buy); }}
  .panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 8px 12px 16px; margin-bottom: 16px; }}
  .row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
  .chart {{ width: 100%; height: auto; display: block; }}
  .grid line {{ stroke: #2a3340; stroke-width: 1; }}
  .axis {{ fill: var(--muted); font-size: 11px; }}
  .val {{ fill: var(--text); font-size: 11px; }}
  .svg-title {{ fill: var(--muted); font-size: 12px; }}
  .zero {{ stroke: #3a4554; stroke-dasharray: 4 4; }}
  .mark-line {{ stroke-width: 1; stroke-dasharray: 3 4; opacity: 0.45; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line); }}
  th {{ color: var(--muted); font-weight: 500; font-size: 12px; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .legend {{ color: var(--muted); font-size: 12px; padding: 4px 8px 0; }}
  .muted {{ color: var(--muted); }}
  @media (max-width: 860px) {{
    .stats, .row {{ grid-template-columns: 1fr; }}
  }}
</style>
</head>
<body>
  <h1>{title}</h1>
  <p class="sub">{subtitle}</p>
  <div class="stats">
    <div class="stat"><div class="v {g_cls}">{gross}</div><div class="l">Gross PnL (USD)</div></div>
    <div class="stat"><div class="v neg">{comm}</div><div class="l">Commission (USD)</div></div>
    <div class="stat"><div class="v {n_cls}">{net}</div><div class="l">Net PnL (USD)</div></div>
    <div class="stat"><div class="v">{fills_n}</div><div class="l">Fills</div></div>
  </div>
  <div class="panel">
    {price_svg}
    <div class="legend">Blue line = 1-minute midpoint close · green triangle = BUY · red triangle = SELL · time axis in ICT</div>
  </div>
  <div class="row">
    <div class="panel">{bar_svg}</div>
    <div class="panel">{eq_svg}</div>
  </div>
  <div class="panel">
    {table}
  </div>
</body>
</html>
"""


def render_table(rounds: list[dict]) -> str:
    if not rounds:
        return "<p class='muted'>No matched round-trips yet</p>"
    rows = []
    for i, r in enumerate(rounds, 1):
        g_cls = "pos" if r["gross"] >= 0 else "neg"
        n_cls = "pos" if r["net"] >= 0 else "neg"
        rows.append(
            "<tr>"
            f"<td>{i}</td>"
            f"<td>{escape(r['symbol'])}</td>"
            f"<td>{r['buy']['dt'].strftime('%H:%M:%S')}</td>"
            f"<td>{r['sell']['dt'].strftime('%H:%M:%S')}</td>"
            f"<td class='num'>{r['hold_sec']:.0f}s</td>"
            f"<td class='num'>{r['qty']:.0f}</td>"
            f"<td class='num'>{r['buy']['price']:.5f}</td>"
            f"<td class='num'>{r['sell']['price']:.5f}</td>"
            f"<td class='num {g_cls}'>{r['gross']:+.2f}</td>"
            f"<td class='num'>{r['commission']:.2f}</td>"
            f"<td class='num {n_cls}'>{r['net']:+.2f}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr>"
        "<th>Round</th><th>Symbol</th><th>Buy (ICT)</th><th>Sell (ICT)</th>"
        "<th class='num'>Hold</th><th class='num'>Qty</th><th class='num'>Buy px</th>"
        "<th class='num'>Sell px</th><th class='num'>Gross</th><th class='num'>Comm</th>"
        "<th class='num'>Net</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def main() -> None:
    ib = IB()
    ib.connect(HOST, PORT, clientId=CLIENT_ID, timeout=60)
    ib.reqMarketDataType(3)

    raw = ib.reqExecutions(ExecutionFilter())
    fills = []
    for f in raw:
        e = f.execution
        cr = f.commissionReport
        fills.append(
            {
                "dt": to_ict(e.time),
                "symbol": f.contract.localSymbol,
                "side": e.side,
                "shares": float(e.shares),
                "price": float(e.price),
                "commission": float(cr.commission) if cr and cr.commission else 0.0,
            }
        )
    fills.sort(key=lambda x: x["dt"])

    symbols = sorted({f["symbol"] for f in fills}) or ["EUR.USD"]
    contract = Forex(symbols[0].replace(".", ""))
    ib.qualifyContracts(contract)
    bars_raw = ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr="1 D",
        barSizeSetting="1 min",
        whatToShow="MIDPOINT",
        useRTH=False,
        formatDate=1,
    )
    ib.disconnect()

    all_bars = [{"dt": to_ict(b.date), "close": float(b.close)} for b in bars_raw]
    if fills:
        tmin = fills[0]["dt"] - timedelta(minutes=20)
        tmax = fills[-1]["dt"] + timedelta(minutes=5)
        bars = [b for b in all_bars if tmin <= b["dt"] <= tmax] or all_bars[-40:]
    else:
        bars = all_bars[-40:]

    rounds = pair_rounds(fills)
    if rounds and all(r["commission"] == 0 for r in rounds):
        # IB demo often omits commission in reqExecutions — use 2 USD per fill, as printed on real fills
        for r in rounds:
            r["commission"] = 4.0
            r["net"] = r["gross"] - 4.0

    gross = sum(r["gross"] for r in rounds)
    comm = sum(r["commission"] for r in rounds)
    net = gross - comm
    cum_g, cum_n = [0.0], [0.0]
    g_run = n_run = 0.0
    for r in rounds:
        g_run += r["gross"]
        n_run += r["net"]
        cum_g.append(g_run)
        cum_n.append(n_run)
    labels = ["Start"] + [f"R{i}" for i in range(1, len(rounds) + 1)]

    html = HTML.format(
        title=escape(f"{symbols[0]} session") if symbols else "Session",
        subtitle=escape(
            f"{len(fills)} fills · {len(rounds)} round-trips · "
            f"Generated {datetime.now(ICT).strftime('%Y-%m-%d %H:%M')} ICT · IB {HOST}:{PORT}"
        ),
        gross=f"{gross:+.2f}",
        comm=f"{-comm:.2f}",
        net=f"{net:+.2f}",
        fills_n=str(len(fills)),
        g_cls="pos" if gross >= 0 else "neg",
        n_cls="pos" if net >= 0 else "neg",
        price_svg=svg_price(bars, fills),
        bar_svg=svg_bars(
            [r["gross"] for r in rounds],
            [f"R{i}" for i in range(1, len(rounds) + 1)],
            "Gross PnL by round (USD)",
        ),
        eq_svg=svg_equity(cum_g, cum_n, labels),
        table=render_table(rounds),
    )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print("wrote", OUT)
    print(json.dumps(
        {
            "fills": len(fills),
            "rounds": len(rounds),
            "gross": round(gross, 2),
            "commission": round(comm, 2),
            "net": round(net, 2),
        },
        indent=2,
    ))


if __name__ == "__main__":
    main()
