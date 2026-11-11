# qos-sdk

Write a trading strategy in one Python file, test it on your machine with the same Nautilus backtest engine QOS uses, and install it into QOS.

```
pip install "qos-sdk @ git+https://github.com/qos-one/qos.git@v0.1.5#subdirectory=qos_sdk"
qos new my_strategy            # or: qos new --ml my_model
cd my_strategy && pytest tests
qos install .
```

```python
from qos.sdk import catalog, quick_backtest
from strategy import MyStrategy

bars = catalog.bars("EUR/USD", "5m", "2026-01-01", "2026-06-30", source="eurusd_5m.parquet")   # file from `qos data export`
print(quick_backtest(MyStrategy, {"fast": 12}, bars)["pnl_usd"])
```

Not on PyPI. Install from GitHub as above, pinned to release v0.1.5. For the ML template and pytest, use `"qos-sdk[ml,test] @ git+https://github.com/qos-one/qos.git@v0.1.5#subdirectory=qos_sdk"`. Full guide: `docs/DEVELOPER.md` in QOS.

## License

The developer kit itself (`qos/sdk`, `qos/templates`) is Apache-2.0. This package also contains parts of the QOS engine
(`qos`, `qx_strategies`) under AGPL-3.0-only, which the backtest needs. The QOS Agent Exception in `NOTICE` lets
strategies that use only `qos.sdk` be released under any license, including proprietary ones. See `LICENSE`,
`LICENSE-AGPL` and `NOTICE`.
