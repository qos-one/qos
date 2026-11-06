from datetime import datetime, timedelta, timezone

import pandas as pd

from api import catalog as cat


def _rows(n, start=datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)):
    return [{"t": start + timedelta(minutes=i), "o": 1.1 + i * 1e-5, "h": 1.1002 + i * 1e-5,
             "l": 1.0998 + i * 1e-5, "c": 1.1001 + i * 1e-5} for i in range(n)]


def test_ids():
    assert cat.instrument_id_str("EURUSD") == "EUR/USD.IDEALPRO"
    assert cat.bar_type_str("USDJPY", "5m") == "USD/JPY.IDEALPRO-5-MINUTE-MID-EXTERNAL"


def test_instrument():
    i = cat.make_fx_instrument("USDJPY")
    assert str(i.id) == "USD/JPY.IDEALPRO" and i.price_precision == 3
    assert cat.make_fx_instrument("EURUSD").price_precision == 5


def test_frame_uses_close_time_and_dedupes():
    rows = _rows(3) + _rows(1)
    df = cat.ib_bars_to_frame(rows)
    assert len(df) == 3
    assert df.index[0] == pd.Timestamp("2026-10-20 10:01", tz="UTC")


def test_resample_5m_closed_right():
    df = cat.ib_bars_to_frame(_rows(10))
    r = cat.resample_closed(df, "5min")
    assert list(r.index) == [pd.Timestamp("2026-10-20 10:05", tz="UTC"), pd.Timestamp("2026-10-20 10:10", tz="UTC")]
    assert r.iloc[0]["open"] == df.iloc[0]["open"] and r.iloc[0]["close"] == df.iloc[4]["close"]


def test_only_new_closed():
    df = cat.ib_bars_to_frame(_rows(10))
    out = cat.only_new_closed(df, pd.Timestamp("2026-10-20 10:03", tz="UTC"), pd.Timestamp("2026-10-20 10:08", tz="UTC"))
    assert out.index.min() == pd.Timestamp("2026-10-20 10:04", tz="UTC")
    assert out.index.max() == pd.Timestamp("2026-10-20 10:08", tz="UTC")


def test_write_and_read_back(tmp_path):
    c = cat.open_catalog(tmp_path)
    df = cat.ib_bars_to_frame(_rows(30))
    assert cat.write_bars(c, "EURUSD", "1m", df) == 30
    assert cat.last_timestamp(c, cat.bar_type_str("EURUSD", "1m")) == df.index.max()


def test_gap_report_ignores_weekend():
    fri = datetime(2026, 10, 23, 20, 58, tzinfo=timezone.utc)
    rows = _rows(2, fri) + _rows(2, datetime(2026, 10, 25, 21, 0, tzinfo=timezone.utc)) + _rows(2, datetime(2026, 10, 26, 1, 0, tzinfo=timezone.utc))
    gaps = cat.gap_report(cat.ib_bars_to_frame(rows))
    assert len(gaps) == 1 and gaps[0]["minutes"] > 200


def test_gap_report_ignores_daily_break_and_holiday():
    rows = _rows(2, datetime(2026, 10, 20, 20, 58, tzinfo=timezone.utc)) + _rows(2, datetime(2026, 10, 20, 21, 15, tzinfo=timezone.utc))
    assert cat.gap_report(cat.ib_bars_to_frame(rows)) == []
    # checked separately: in one frame the 10-month jump 2025-12-26 (a Friday) → 2026-10-20 is a real gap; it used to be
    # excused only because any gap > 24 h starting on a Friday counted as "weekend" (QA ops finding)
    rows = _rows(2, datetime(2025, 12, 24, 21, 58, tzinfo=timezone.utc)) + _rows(2, datetime(2025, 12, 26, 0, 0, tzinfo=timezone.utc))
    assert cat.gap_report(cat.ib_bars_to_frame(rows)) == []


def test_rewrite_series_roundtrip(tmp_path):
    c = cat.open_catalog(tmp_path)
    df = cat.ib_bars_to_frame(_rows(22))
    cat.write_bars(c, "EURUSD", "1m", df.iloc[:10])
    back = cat.read_bars(c, "EURUSD")
    assert len(back) == 10
    merged = pd.concat([back, df.iloc[10:]])
    n1, n5 = cat.rewrite_series(cat.open_catalog(tmp_path), tmp_path, "EURUSD", merged)
    assert n1 == 22 and n5 == 4   # 5th 5m bar is incomplete → not written
    assert len(cat.read_bars(cat.open_catalog(tmp_path), "EURUSD")) == 22
