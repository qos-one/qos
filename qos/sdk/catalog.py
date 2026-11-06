"""
Price data for developers — read from the local QOS data catalog or from a parquet file exported with `qos data export`

  from qos.sdk import catalog
  bars = catalog.bars("EUR/USD", "5m", "2026-07-01", "2026-10-01")                  # local catalog (QOS_CATALOG)
  bars = catalog.bars("EUR/USD", "5m", source="eurusd_5m.parquet")                  # file from qos data export
  bars = catalog.bars("EUR/USD", "5m", source="cloud:fx-1m")                        # dataset from QOS Cloud (qos data pull fx-1m)

Returns Nautilus bars (Bar), the same ones QOS backtests use · bar time = bar close time (UTC)
"""
from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pandas as pd

TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30}


def default_dir() -> Path:
    """QOS_CATALOG · the local QOS data/catalog folder (see qos/paths.py) · otherwise ./data/catalog"""
    from qos import paths
    return paths.catalog_dir()


CATALOG_DIR = default_dir()


def instrument_id(instrument: str) -> str:
    """'EURUSD' / 'EUR/USD' / 'EUR/USD.IDEALPRO' → 'EUR/USD.IDEALPRO'"""
    s = instrument.strip().upper()
    if "." in s:
        return s
    if "/" not in s and len(s) == 6:
        s = f"{s[:3]}/{s[3:]}"
    return f"{s}.IDEALPRO"


def tf_minutes(tf) -> int:
    if isinstance(tf, int):
        return tf
    if str(tf) not in TF_MINUTES:
        raise ValueError(f"timeframe must be one of {', '.join(TF_MINUTES)}")
    return TF_MINUTES[str(tf)]


def make_fx_instrument(symbol: str):
    """CurrencyPair for IDEALPRO: 5 decimals (JPY 3), quantity in base-currency units, 0.2 bps commission like IBKR"""
    from nautilus_trader.model.currencies import USD
    from nautilus_trader.model.identifiers import InstrumentId, Symbol
    from nautilus_trader.model.instruments import CurrencyPair
    from nautilus_trader.model.objects import Currency, Money, Price, Quantity

    prec = 3 if symbol.endswith("JPY") else 5
    return CurrencyPair(
        instrument_id=InstrumentId.from_str(f"{symbol[:3]}/{symbol[3:]}.IDEALPRO"),
        raw_symbol=Symbol(f"{symbol[:3]}.{symbol[3:]}"),
        base_currency=Currency.from_str(symbol[:3]),
        quote_currency=Currency.from_str(symbol[3:]),
        price_precision=prec,
        size_precision=0,
        price_increment=Price(1 / 10**prec, prec),
        size_increment=Quantity.from_int(1),
        lot_size=Quantity.from_int(1),
        max_quantity=Quantity.from_str("1e8"),
        min_quantity=Quantity.from_int(1),
        max_notional=None,
        min_notional=Money(1.00, USD),
        max_price=None,
        min_price=None,
        margin_init=Decimal("0.03"),
        margin_maint=Decimal("0.03"),
        maker_fee=Decimal("0.00002"),
        taker_fee=Decimal("0.00002"),
        ts_event=0,
        ts_init=0,
    )


def resample_closed(df1m: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample 1-minute bars (index = close time) into larger bars — right label/closed, so the index is the larger bar's close time"""
    if df1m.empty:
        return df1m
    out = df1m.resample(rule, label="right", closed="right").agg({"open": "first", "high": "max", "low": "min", "close": "last"})
    return out.dropna()


def open_catalog(path: Path | None = None):
    from nautilus_trader.persistence.catalog import ParquetDataCatalog
    path = Path(path or default_dir())
    path.mkdir(parents=True, exist_ok=True)
    return ParquetDataCatalog(str(path))


def frame_to_bars(df: pd.DataFrame, instrument: str, tf) -> list:
    """DataFrame (index = close time UTC, open/high/low/close[, volume]) → Nautilus bars"""
    from nautilus_trader.model.data import BarType
    from nautilus_trader.persistence.wranglers import BarDataWrangler
    from qx_strategies import bar_type_str
    iid = instrument_id(instrument)
    df = df[["open", "high", "low", "close"] + (["volume"] if "volume" in df else [])].astype(float).copy()
    if "volume" not in df:
        df["volume"] = 1_000_000.0
    df.index.name = "timestamp"
    sym = iid.split(".")[0].replace("/", "")
    return BarDataWrangler(BarType.from_str(bar_type_str(iid, tf_minutes(tf), "EXTERNAL")), make_fx_instrument(sym)).process(df)


def bars_to_frame(bars: list) -> pd.DataFrame:
    return pd.DataFrame({"open": [b.open.as_double() for b in bars], "high": [b.high.as_double() for b in bars],
                         "low": [b.low.as_double() for b in bars], "close": [b.close.as_double() for b in bars],
                         "volume": [b.volume.as_double() for b in bars]},
                        index=pd.to_datetime([b.ts_event for b in bars], unit="ns", utc=True)).rename_axis("timestamp")


def bars(instrument: str, tf="5m", start=None, end=None, source: str | Path | None = None) -> list:
    """Bars from a catalog (folder) or a `qos data export` parquet file · timeframes the catalog lacks are built from 1-minute bars"""
    s = pd.Timestamp(start, tz="UTC") if start is not None and pd.Timestamp(start).tzinfo is None else (pd.Timestamp(start) if start is not None else None)
    e = pd.Timestamp(end, tz="UTC") if end is not None and pd.Timestamp(end).tzinfo is None else (pd.Timestamp(end) if end is not None else None)
    if isinstance(source, str) and source.startswith("cloud:"):
        from qos.cloud.data import resolve
        source = resolve(source[len("cloud:"):])
    src = Path(source).expanduser() if source else default_dir()
    if src.is_file():
        df = read_export(src)
        meta = df.attrs
        if meta.get("instrument") and instrument_id(instrument) != meta["instrument"]:
            raise ValueError(f"{src.name} holds {meta['instrument']}, not {instrument_id(instrument)}")
        if s is not None:
            df = df[df.index >= s]
        if e is not None:
            df = df[df.index <= e]
        have = int(meta.get("tf_minutes") or tf_minutes(tf))
        want = tf_minutes(tf)
        if want != have:
            if want % have:
                raise ValueError(f"{src.name} is {have}m — cannot make {want}m bars from it")
            df = resample_closed(df[["open", "high", "low", "close"]], f"{want}min")
        return frame_to_bars(df, instrument, want)
    from qx_strategies.backtest import load_bars
    naive = lambda t: t.tz_convert("UTC").tz_localize(None) if t is not None else None   # noqa: E731  (load_bars treats them as UTC)
    return load_bars({"instrument": instrument_id(instrument), "tf_minutes": tf_minutes(tf)}, naive(s), naive(e), src)


def export(instrument: str, tf, path: str | Path, start=None, end=None, catalog: str | Path | None = None) -> dict:
    """Write bars to a single parquet file (to share with developers without QOS) — stores instrument / timeframe in the metadata"""
    import pyarrow as pa
    import pyarrow.parquet as pq
    bs = bars(instrument, tf, start, end, catalog)
    if not bs:
        raise ValueError(f"no bars for {instrument_id(instrument)} {tf} in {catalog or default_dir()}")
    df = bars_to_frame(bs)
    table = pa.Table.from_pandas(df)
    meta = {**(table.schema.metadata or {}), b"qos": f'{{"instrument": "{instrument_id(instrument)}", "tf_minutes": {tf_minutes(tf)}}}'.encode()}
    path = Path(path).expanduser()
    pq.write_table(table.replace_schema_metadata(meta), path)
    return {"path": str(path), "bars": len(df), "from": df.index[0].isoformat(), "to": df.index[-1].isoformat(),
            "instrument": instrument_id(instrument), "tf_minutes": tf_minutes(tf)}


def read_export(path: str | Path) -> pd.DataFrame:
    import json

    import pyarrow.parquet as pq
    t = pq.read_table(path)
    df = t.to_pandas()
    if "timestamp" in df.columns:
        df = df.set_index("timestamp")
    df.index = pd.to_datetime(df.index, utc=True)
    raw = (t.schema.metadata or {}).get(b"qos")
    df.attrs = json.loads(raw) if raw else {}
    return df.sort_index()
