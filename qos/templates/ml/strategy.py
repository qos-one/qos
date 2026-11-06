"""$label — logistic regression on recent returns (model.joblib, trained by train.py)"""
from qos.sdk import Param, Strategy, sibling

features = sibling(__file__, "features")


class $Class(Strategy):
    name = "$name"
    version = "0.1.0"
    instruments = ["EUR/USD"]
    timeframe = "5m"
    summary = "p > {threshold}"

    threshold = Param(0.55, min=0.5, max=0.95, step=0.01, grid=[0.52, 0.55, 0.6],
                      help="probability needed to go long (short below 1 − threshold)")

    def on_start(self):
        self.model = self.load_model("model.joblib")

    def on_bar(self, bar):
        bars = self.bars(features.WINDOW + 1)
        if len(bars) < features.WINDOW + 1:
            return
        x = features.make_features([float(b.close) for b in bars])
        p_up = float(self.model.predict_proba([x])[0][1])
        th = self.p.threshold
        self.target(+1 if p_up > th else -1 if p_up < 1 - th else 0)
