# $name

$label — a QOS ML strategy package (`qos new --ml $name`).

```
pip install "qos-sdk[ml,test] @ git+https://github.com/qos-one/qos.git@v0.1.5#subdirectory=qos_sdk"
python train.py --data eurusd_5m.parquet --end 2026-07-01     # or --synthetic
pytest tests
qos install .
```

- `features.py` — features used by both training and live trading (one function, so they can't drift apart)
- `train.py` — trains `model.joblib`; train on data **before** the period you judge the strategy on (QOS evaluates the last 90 days)
- `strategy.py` — loads the model with `self.load_model("model.joblib")` in `on_start`, predicts in `on_bar`
- The model file ships with the package. Retrained model = new `version`.

Guide: docs/DEVELOPER.md in QOS.
