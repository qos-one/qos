"""Train the model → model.joblib (ships with the package)

  python train.py --synthetic                                         no data needed (what `qos new --ml` runs)
  python train.py --data eurusd_5m.parquet --end 2026-07-01           file from `qos data export`
  python train.py --instrument EUR/USD --start 2025-10-01 --end 2026-07-01   local QOS data catalog
"""
import argparse
from pathlib import Path

import joblib
from sklearn.linear_model import LogisticRegression

from features import WINDOW, make_features


def dataset(closes: list[float]) -> tuple[list, list]:
    """features at bar i (only bars ≤ i) → label: does bar i + 1 close higher?"""
    X, y = [], []
    for i in range(WINDOW, len(closes) - 1):
        X.append(make_features(closes[i - WINDOW:i + 1]))
        y.append(int(closes[i + 1] > closes[i]))
    return X, y


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--data", default=None, help="parquet from `qos data export`")
    ap.add_argument("--instrument", default="EUR/USD")
    ap.add_argument("--tf", default="5m")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    a = ap.parse_args()
    if a.synthetic:
        from qos.sdk.testing import synthetic_bars
        bars = synthetic_bars("EUR/USD.IDEALPRO", 5, n=4000)
    else:
        from qos.sdk import catalog
        bars = catalog.bars(a.instrument, a.tf, a.start, a.end, source=a.data)
    closes = [float(b.close) for b in bars]
    X, y = dataset(closes)
    model = LogisticRegression(max_iter=1000).fit(X, y)
    joblib.dump(model, Path(__file__).with_name("model.joblib"))
    print(f"trained on {len(X)} samples ({bars[0].bar_type}) · in-sample accuracy {model.score(X, y):.3f} → model.joblib")


if __name__ == "__main__":
    main()
