"""Features used by BOTH train.py and strategy.py — keep them identical, or live trading will differ from training"""
WINDOW = 12


def make_features(closes: list[float]) -> list[float]:
    """closes: the last WINDOW + 1 closing prices (old → new) → WINDOW one-bar returns in basis points"""
    c = closes[-(WINDOW + 1):]
    return [(c[i + 1] / c[i] - 1) * 10_000 for i in range(WINDOW)]
