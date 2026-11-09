# QOS — the Quant Operating System

QOS is open-source software for running, verifying and controlling systematic trading
strategies on your own server, with your own Interactive Brokers account. You open its
Terminal in any browser.

This repository contains the **engine**: the API, NautilusTrader execution, the risk engine,
backtest and walk-forward tools, the `qos` command-line tool, the MCP server for AI assistants
and the Python developer kit (SDK). The Terminal is in
[qos-terminal](https://github.com/qos-one/qos-terminal) and example strategies are in
[qos-examples](https://github.com/qos-one/qos-examples).

> QOS is trading software, not investment advice. Trading involves substantial risk of loss.
> Start with an IBKR **paper** account. See [NOTICE](NOTICE).

## What's inside

| Path | What it is | License |
|---|---|---|
| `api/` | API used by the Terminal: nodes, orders, positions, risk, history | AGPL-3.0-only |
| `qx_strategies/` | Strategy runtime shared by backtest and live (one code path) | AGPL-3.0-only |
| `qos/` | `qos` CLI, package install and checks, QOS Cloud client | AGPL-3.0-only |
| `qos/sdk/`, `qos/templates/`, `qos_sdk/` | Developer kit for writing your own strategies | Apache-2.0 |
| `scripts/` | Backtest, walk-forward, data catalog and maintenance tools | AGPL-3.0-only |
| `deploy/` | Server installer, systemd services, Caddy sites, operations guide | AGPL-3.0-only |
| `mcp_server.py` | MCP server: AI assistants read the account and trade within your limits | AGPL-3.0-only |

## Install on a server (recommended)

One script installs the engine, Redis, IB Gateway and the web Terminal on an Ubuntu 24.04+
server, so strategies keep trading when your computer is off:

```bash
curl -fsSLO https://raw.githubusercontent.com/qos-one/qos/main/deploy/install.sh
less install.sh                     # read it first: it runs as root
sudo QOS_TERMINAL_DOMAIN=terminal.example.com ACME_EMAIL=you@example.com bash install.sh
```

Then open `https://terminal.example.com`, sign in and connect your IBKR paper account. The full
guide is [docs/SELF_HOSTING.md](docs/SELF_HOSTING.md); updates, IB Gateway, the web Terminal and
remote AI access are in [deploy/README.md](deploy/README.md).

## Run it for development

Requirements:

- Linux, Python 3.13 and [uv](https://docs.astral.sh/uv/)
- Docker, for Redis (`docker-compose.yml`) and, optionally, IB Gateway
- IB Gateway or TWS with an IBKR paper account
- The Interactive Brokers API, installed from PyPI by `uv sync`. It has its own license — see
  [NOTICE](NOTICE).

```bash
uv sync
docker compose up -d                 # Redis on 127.0.0.1:6379
uv run qos home init ~/qos           # folders, .env (from .env.example) and example configs
export QOS_HOME=~/qos                # add this to your shell profile
uv run qos home show                 # prints every resolved folder
uv run uvicorn api.server:app --port 8000
```

Check it is up with `curl localhost:8000/health`, then run the
[Terminal](https://github.com/qos-one/qos-terminal) against it. Without `QOS_HOME`, QOS keeps
its files next to the code (`data/`, `configs/`, `reports/`).

## QOS Cloud

The `qos login`, `qos store`, `qos data pull` and other Cloud commands are clients for a hosted service that is not open
to the public yet: this release points them at `http://localhost:8090` and ships no production signing keys, so they do
nothing outside a development setup. Everything else (engine, Terminal, strategies, backtests, live trading on your own
IBKR account) works without it.

## AI assistants (MCP)

`mcp_server.py` lets an AI assistant read the account and place orders through the same risk
checks as the Terminal, as an operator with role `assistant`: paper accounts only unless you turn
on live, with limits on order size and orders per hour. On a server, `deploy/mcp-setup.sh` opens
it at `https://mcp.<your-domain>/mcp` for clients such as Grok or ChatGPT — see
[deploy/README.md](deploy/README.md).

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
