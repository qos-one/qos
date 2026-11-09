# QOS Strategy Developer Guide

> Write one Python file → test it locally on the same engine QOS uses → `qos install` → QOS evaluates it and gates it all the way to live.

---

## 1. The whole flow

```
pip install "qos-sdk @ git+https://github.com/qos-one/qos.git#subdirectory=qos_sdk"   ← no QOS needed
qos new my_strategy                  ← or qos new --ml my_model
cd my_strategy && pytest tests       ← synthetic bars, no data needed
(real data) qos data export …        ← someone with QOS exports a parquet file
qos install .                        ← 5 install checks
──────────── on the QOS machine ────────────
installed → backtested → wf_passed   ← automatic after install
Deploy (paper) → soak 5 trading days ← Terminal › Strategies › Deploy
soak GO → Allow live                 ← Terminal › Strategies › Nodes using … › Allow live
```

| Step | Who | Where |
|---|---|---|
| Write + test | Developer | Own machine (the SDK, see Install) |
| `qos install` | Developer or QOS operator | QOS machine (or own machine = checks only) |
| Backtest / walk-forward | QOS, automatically | Strategy lab job |
| Deploy paper, soak, Allow live | QOS operator | Terminal |

## 2. Install

```
python -m venv .venv && source .venv/bin/activate
pip install "qos-sdk @ git+https://github.com/qos-one/qos.git#subdirectory=qos_sdk"
qos --help
```

The SDK is not on PyPI yet: install it from GitHub as above (add `[ml]` for the ML template, `[test]` for pytest:
`"qos-sdk[ml,test] @ git+https://github.com/qos-one/qos.git#subdirectory=qos_sdk"`). Pin a release with `@v0.1.3`
after `qos.git`.

```
```

Python ≥ 3.12. qos-sdk installs NautilusTrader 1.230 (the same version QOS uses), so a local backtest gives the same result as QOS on the same bars
(proven by the test `test_quick_backtest_equals_qos_backtest`).

On a machine that already runs QOS there is no need to install it: run `uv run qos` in the QOS folder.

## 3. First strategy

`qos new my_ema` creates:

```
my_ema/
  strategy.py           EMA crossover, ~25 lines, runs as is
  tests/test_strategy.py
  README.md
```

```python
from qos.sdk import Param, Strategy


class MyEma(Strategy):
    name = "my_ema"                 # a-z 0-9 _ · must match the installed folder name
    version = "0.1.0"               # MAJOR.MINOR.PATCH
    instruments = ["EUR/USD"]       # orders only for declared instruments
    timeframe = "5m"                # 1m 5m 15m 30m 1h
    summary = "EMA {fast}/{slow}"

    fast = Param(10, min=2, max=100, grid=[10, 20])
    slow = Param(30, min=5, max=300, grid=[30, 50])

    def on_configure(self):
        if self.p.fast >= self.p.slow:
            raise ValueError("fast must be smaller than slow")

    def on_start(self):
        self.fast_ema = self.indicator("ema", self.p.fast)
        self.slow_ema = self.indicator("ema", self.p.slow)

    def on_bar(self, bar):
        if self.ready():
            self.target(+1 if self.fast_ema.value > self.slow_ema.value else -1)
```

Test on real data:

```python
from qos.sdk import catalog, quick_backtest
from strategy import MyEma

bars = catalog.bars("EUR/USD", "5m", "2026-01-01", "2026-06-30", source="eurusd_5m.parquet")   # or source=<catalog folder>
r = quick_backtest(MyEma, {"fast": 12, "slow": 40}, bars)
print(r["pnl_usd"], r["closed_positions"], r["max_drawdown_usd"])
```

Data: on the QOS machine run `qos data export EUR/USD 5m --days 365 -o eurusd_5m.parquet` and send the file to the developer
(the file records the instrument / timeframe · 5m is resampled to 15m/30m/1h automatically).

Data from the QOS Cloud data store: `qos login` → `qos data list` → `qos data pull fx-1m`, then use `catalog.bars("EUR/USD", "5m", source="cloud:fx-1m")` · available according to your account's entitlements (works offline for 7 days).

## 4. The Strategy contract

### Metadata (class attributes)

| Name | Default | Meaning |
|---|---|---|
| `name`, `version` | — | Package identity `name@version` |
| `label`, `description`, `summary` | — | Shown in the Terminal · `summary` may use `{param}` |
| `instruments` | — | FX on IDEALPRO, e.g. `"EUR/USD"` · orders outside this list are rejected |
| `timeframe` | `"5m"` | `1m` `5m` `15m` `30m` `1h` |
| `requires` | `[]` | pip requirements → QOS creates a separate venv (`<package>/.venv`) |
| `timer_interval_s` | `0` | > 0 → `on_timer` is called every N seconds |
| `max_drawdown_pct` | `10` | Drawdown ceiling for the backtest criteria (% of 100,000 capital) |
| `max_notional_usd` | `500000` | Maximum position value per instrument |
| `max_orders_per_hour` | `60` | Orders that open/increase a position per hour |
| `max_flips_per_hour` | `30` | long↔short flips per hour |
| `max_memory_mb` | `2048` | Node memory limit |

`Param(default, min=, max=, step=, grid=, label=, help=)` — the type comes from the default value (int / float / bool / str) ·
values the user edits in the Terminal are node overrides (the package is not changed) · `grid` = the values walk-forward tries

### Hooks

| Hook | When | Use it for |
|---|---|---|
| `on_configure()` | Before `on_start`, at construction | Validate parameter relationships → `raise ValueError` |
| `on_start()` | Once at start | Create indicators, `self.load_model()` |
| `on_bar(bar)` | Every closed bar | Decide → `self.target(...)` · `float(bar.close)` |
| `on_timer(interval_s)` | Every `timer_interval_s` | Work not tied to bars |
| `on_stop()` | Node stops | Clean up. Afterwards QOS cancels the strategy's orders and, while the market is open, closes the positions this node opened (see [When a node stops](#when-a-node-stops)) |

### When a node stops

- QOS cancels the strategy's working orders. Afterwards the engine checks IBKR: any order of that node still working
  is cancelled under the node's own IBKR client ID, and the Terminal (and Telegram, when set up) warns if one survives.
- While the market is open it closes the positions this node opened (`close_positions_on_stop`, on by default).
- With FX closed (Friday 17:00 to Sunday 17:00 New York) the position is kept and the Terminal shows why; strategies
  also send no new orders in that window, and never stack a new entry on an earlier one that has not filled.
- A node that restarts starts flat and does not know positions from before; QOS pauses its new entries until an
  operator confirms the positions in the Terminal.

### Services

| | |
|---|---|
| `self.target(side)` | +1 long · 0 flat · −1 short (size = the node's trade_size) · returns True if an order was sent |
| `self.target_qty(qty)` | Hold qty units (partial increase/decrease allowed, e.g. grid) |
| `self.position()` | Net position (+/−) |
| `self.indicator(kind, *args)` | Nautilus `ema` `sma` `rsi` `atr` `bollinger`, updated automatically (`rsi.value` is in the 0–1 range) |
| `self.ready()` | All indicators have finished warm-up |
| `self.bars(n)` | Last n bars (oldest → newest, including the current bar) |
| `self.state` | dict that QOS persists across restarts (JSON) |
| `self.p` | Parameter values (read-only) |
| `self.publish(name, **data)` | Signal to the Terminal and soak |
| `self.load_model(file)` | joblib/pickle from the package folder |
| `sibling(__file__, "features")` | Import a file next to strategy.py without clashing with other packages |
| `self.trade_size`, `self.tf_minutes`, `self.allow_short`, `self.instrument`, `self.log` | Node information |

QOS handles for you (no code needed): account risk, kill switch, node SL/TP, closing positions on stop, reconciliation, attribution, state persistence.

## 5. What is forbidden

| Don't | Result |
|---|---|
| Use the network (socket, HTTP, DNS) at import time or in a hook | `qos install` fails the **network** check |
| Use unseeded randomness, the system clock (`datetime.now()`), external state | Fails the **deterministic** check |
| Use future data (bars not yet closed / preloaded data) | Fails the **lookahead** check (only direct cases are detected) |
| Wrong metadata, default parameters cannot build the strategy | Fails the **manifest** check |
| Write files outside the package folder / `data/strategy_state` | `PermissionError` at run time |
| Order an undeclared instrument, exceed the notional / orders / flips ceilings | Order rejected · more than 3 times in 10 minutes = QOS stops the node |
| Use more memory than `max_memory_mb` | QOS stops the node |
| Touch `self._ctx` or Nautilus directly | Unsupported — may break when QOS is updated |

## 6. Lifecycle

```
installed ──► backtested ──► wf_passed ──► soak_go ──► live
```

| Status | Criteria (automatic · can be demoted) |
|---|---|
| installed | Passed the 5 checks · can be deployed as a **paper** node |
| backtested | Backtest over the last 90 days: net > 0 and max DD ≤ `max_drawdown_pct` |
| wf_passed | 365-day walk-forward (train 90 / test 30, grid from the manifest) with positive out-of-sample results in ≥ 60% of folds |
| soak_go | A node running this version got a soak verdict of GO (paper, 5 trading days: parity, slippage, latency, no crashes) |
| live | The operator clicks **Allow live** (soak_go only) |

Check status: `qos status` or Terminal › Strategies › Packages.

## 7. Releasing a new version

1. Change the code / retrain the model → bump `version` (e.g. `1.0.0` → `1.1.0`)
2. `pytest tests` → `qos install .` — the old version stays installed and existing nodes keep running their pinned version
3. The new version starts at installed and must pass every criterion again (results of the old version do not count)
4. Move nodes: deploy a new node from the new version, or restart a node that refers to the bare name (`my_ema` = latest version)
5. Remove the old version once no node uses it: `qos uninstall my_ema@1.0.0`
6. `--force` reinstalls over an existing version only when no node uses it, and the status restarts at installed

## 8. ML

`qos new --ml my_model` → `features.py`, `train.py`, `model.joblib` (pre-trained on synthetic data), and a `strategy.py` that calls `load_model` in `on_start`.

- Use the same features function for training and at run time (`sibling(__file__, "features")`)
- Train on data from **before** the period QOS evaluates (the last 90 days), otherwise the backtest results fool you
- The model ships with the package — retraining = a new version
- Libraries QOS does not have → `requires = ["lightgbm==4.5.0"]` (separate venv)

Examples: `rsi_reversion` in [qos-examples](https://github.com/qos-one/qos-examples) (rule-based) and the ML template (`qos new <name> --ml`).

## 9. Known limits

- FX on IBKR IDEALPRO only, timeframe ≤ 1h, one instrument per strategy per node
- The lookahead check only catches direct use of future bars
- Nodes still receive the IB Gateway host/port (no credential proxy yet) — limits are enforced mainly in the SDK/node
- The file-write restriction covers Python code only (it is not an OS-level sandbox)


## Publishing to QOS Cloud

1. Sign up as a developer (`/signup`, tick "I build agents"), then `qos login`.
2. `qos publish ./my_agent --title "My agent" --description "…"` — scans the code first; add `--check` to run the full Cloud checks (backtest + walk-forward) on your machine before uploading.
3. `qos agent status my_agent` — QOS Cloud re-runs the checks on its own data, then a reviewer approves or rejects with notes.
4. New version = bump `version` in `strategy.py` (versions cannot be replaced).

Rules for marketplace agents (checked automatically):
- no network, processes, `ctypes`, `eval`/`exec`/`compile`/`__import__`, `getattr`/`setattr`, environment variables, `sys`, `importlib`, `pickle`; no dunder tricks (`__subclasses__`, `__globals__`, …)
- files: `.py .json .toml .yaml .md .txt .csv .joblib .parquet` · ≤ 200 files · ≤ 50 MB · no symlinks · `tests/` is not uploaded
- `requires = [...]` is not supported yet (the review sandbox has no internet)
- model files (`.joblib`) are allowed but flagged for manual review


### Buyers get a compiled build

By default QOS compiles your approved agent with Cython for each buyer platform (e.g. `cp313-linux-x86_64`); buyers never receive your `.py` source. Publish with `--source` if you want to ship readable source instead.

To keep your agent compatible with compilation:
- load helper modules with `sibling(__file__, "features")` and models with `self.load_model(...)` (both work compiled)
- keep all `.py` files next to `strategy.py` (no sub-folders)
- avoid `inspect.getsource`, monkey-patching your own module, or reading your own `.py` files at run time

Compilation makes code much harder to read but is not perfect protection.

Paid agents: buyers get a licence bound to their device, valid for the paid period (renewed automatically while online, at most 30 days offline).
