# $name

$label — a QOS strategy package (`qos new $name`).

```
pip install "qos-sdk[test] @ git+https://github.com/qos-one/qos.git@v0.1.7#subdirectory=qos_sdk"
python first_backtest.py   # first backtest, no broker account needed (synthetic bars: checks your install)
pytest tests
qos install .
```

- Own data: `python first_backtest.py my_bars.csv` (5m OHLC: `time,open,high,low,close`; time = bar close, UTC).
  A backtest replays past bars: not a forecast, and costs are best case.

- Edit `strategy.py` (parameters, `on_bar`). Re-run `pytest tests`.
- Real data: `from qos.sdk import catalog, quick_backtest` → `quick_backtest($Class, bars=catalog.bars("EUR/USD", "5m", "2026-01-01", "2026-06-30", source="eurusd_5m.parquet"))`
- New version: bump `version` in `strategy.py`, then `qos install .` again (the old version keeps running until you switch nodes).

Guide: docs/DEVELOPER.md in QOS.
