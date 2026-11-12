# $name

$label — a QOS strategy package (`qos new $name`).

```
pip install "qos-sdk[test] @ git+https://github.com/qos-one/qos.git@v0.1.6#subdirectory=qos_sdk"
pytest tests
qos install .
```

- Edit `strategy.py` (parameters, `on_bar`). Re-run `pytest tests`.
- Real data: `from qos.sdk import catalog, quick_backtest` → `quick_backtest($Class, bars=catalog.bars("EUR/USD", "5m", "2026-01-01", "2026-06-30", source="eurusd_5m.parquet"))`
- New version: bump `version` in `strategy.py`, then `qos install .` again (the old version keeps running until you switch nodes).

Guide: docs/DEVELOPER.md in QOS.
