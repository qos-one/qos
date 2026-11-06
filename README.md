# QOS — the Quant Operating System

QOS is open-source software for running, verifying and controlling systematic trading
strategies on your own computer, with your own Interactive Brokers account.

This repository contains the **engine**: the local API, NautilusTrader execution, the risk
engine, backtest and walk-forward tools, the `qos` command-line tool and the Python developer
kit (SDK). The desktop app is in [qos-terminal](https://github.com/qos-one/qos-terminal) and
example strategies are in [qos-examples](https://github.com/qos-one/qos-examples).

> QOS is trading software, not investment advice. Trading involves substantial risk of loss.
> Start with an IBKR **paper** account. See [NOTICE](NOTICE).

## What's inside

| Path | What it is | License |
|---|---|---|
| `api/` | Local API used by the Terminal: nodes, orders, positions, risk, history | AGPL-3.0-only |
| `qx_strategies/` | Strategy runtime shared by backtest and live (one code path) | AGPL-3.0-only |
| `qos/` | `qos` CLI, package install and checks, QOS Cloud client | AGPL-3.0-only |
| `qos/sdk/`, `qos/templates/`, `qos_sdk/` | Developer kit for writing your own strategies | Apache-2.0 |
| `scripts/` | Backtest, walk-forward, data catalog and maintenance tools | AGPL-3.0-only |

## Requirements

- macOS 13+ (Apple silicon) or Linux
- Python 3.13 and [uv](https://docs.astral.sh/uv/)
- Docker, for Redis (`docker-compose.yml`) and, optionally, IB Gateway
- IB Gateway or TWS with an IBKR paper account
- The Interactive Brokers API, installed from PyPI by `uv sync`. It has its own license — see
  [NOTICE](NOTICE).

## Quick start

```bash
uv sync
docker compose up -d                 # Redis on 127.0.0.1:6379
uv run qos home init ~/qos           # folders, .env (from .env.example) and example configs
export QOS_HOME=~/qos                # add this to your shell profile
uv run qos home show                 # prints every resolved folder
uv run uvicorn api.server:app --port 8000
```

Check it is up with `curl localhost:8000/health`, then open the QOS Terminal. Without
`QOS_HOME`, QOS keeps its files next to the code (`data/`, `configs/`, `reports/`).

## Market data

Backtests, walk-forward and the Store checks read bars from a local NautilusTrader data
catalog. Build one from your IBKR account (IB Gateway must be running):

```bash
uv run scripts/build_catalog.py                  # FX 1m + 5m, one year back (10–30 minutes)
uv run scripts/build_catalog.py --update         # add the latest bars
```

Then backtest the built-in EMA cross strategy:

```bash
uv run scripts/backtest.py configs/strategies/ema_cross_eurusd_5m.json --days 90
uv run scripts/walkforward.py configs/strategies/ema_cross_eurusd_5m.json
```

## Write your own strategy

```bash
uv run qos new my_strategy                          # basic template
uv run qos new my_model --ml                        # machine-learning template
uv run python -m qos.publish_check my_strategy      # the Store checks (needs the data catalog;
                                                    # add --scan-only for the code scan alone)
uv run qos install my_strategy                      # install into your local QOS
```

The SDK is Apache-2.0, and the engine grants an additional permission (the QOS Agent
Exception in [NOTICE](NOTICE)), so strategies that use only the SDK can be released under
any license, including proprietary ones. See [docs/DEVELOPER.md](docs/DEVELOPER.md).

## Tests

```bash
uv run pytest -q
```

Some tests need a local data catalog or an IB Gateway and are skipped without them.

## Contributing, security and trademarks

- [CONTRIBUTING.md](CONTRIBUTING.md) — how to contribute (commits need a DCO sign-off)
- [SECURITY.md](SECURITY.md) — report security issues privately, not as public issues
- [TRADEMARKS.md](TRADEMARKS.md) — "QOS", "qos.one" and the QOS logo are trademarks; forks use
  their own name

## License

AGPL-3.0-only, except the developer kit (Apache-2.0). See [LICENSE](LICENSE) and
[NOTICE](NOTICE).
