# Contributing to QOS

Thank you for helping build the open-source Quant Operating System. This repository holds the
engine, the local API used by the QOS Terminal, the `qos` command-line tool and the developer kit.

The Terminal lives in [qos-terminal](https://github.com/qos-one/qos-terminal). Example strategies
live in [qos-examples](https://github.com/qos-one/qos-examples).

QOS is trading software, not investment advice. Trading involves substantial risk of loss.
Start with an IBKR **paper** account. Issues, pull requests and reviews are for **technical** help
only — please do not ask for, and do not expect, buy/sell or position-sizing advice.

## Ways to help

- **Tell us how far you got** — an [issue](https://github.com/qos-one/qos/issues/new) titled
  "Setup report" (it worked or you got stuck) is the most useful first-run signal we have.
  Say your OS, Python version, QOS version and the last step that worked. Please redact
  tokens, account numbers and P/L first.
- **Report a bug** — open an issue with what you did, what you expected, what happened, your OS,
  Python version and the QOS version (`uv run qos --version` or the git commit). Redact credentials.
- **Fix a bug or improve docs** — small pull requests are reviewed fastest.
- **Propose a feature** — open an issue first so we can agree on the design before you write code.
- **Share an example strategy** — open-source examples are welcome in `qos-examples`. A hosted
  Marketplace is not open to the public yet; do not treat this repo as a place to sell an agent.

## Set up (what actually works)

Requirements for everyday engine work:

- Linux (CI runs on Ubuntu 24.04). macOS and Windows are not covered by CI today.
- Python **3.13** and [uv](https://docs.astral.sh/uv/)
- Git

Docker, Redis, IB Gateway/TWS and a data catalog are **not** required to install the package or to
run the default test suite and the broker-free strategy smoke test. You need Redis (the README uses
`docker compose up -d`) to run the API / Terminal, and an IBKR **paper** session to build a market-data
catalog.

```bash
git clone https://github.com/qos-one/qos.git
cd qos
uv sync
uv run qos --version          # e.g. qos 0.1.6
uv run pytest -q              # engine tests; some tests skip without a catalog or IB Gateway
```

`qos-sdk` is **not on PyPI** yet. `pip install qos-sdk` does not work. To work on the engine, use
this clone with `uv run`. Outside this clone (for example the README that `qos new` generates, or
`qos-examples`), install the developer kit from a release tag on GitHub:
`pip install "qos-sdk[test] @ git+https://github.com/qos-one/qos.git@v0.1.6#subdirectory=qos_sdk"`.

### Broker-free first strategy (no IBKR, no Docker, no catalog)

```bash
uv run qos new my_ema
uv run --project . pytest my_ema/tests -q
```

`qos new` writes the package into the **current directory**. If you run it inside this checkout,
`my_ema/` appears in the engine tree and is not gitignored — do not commit it. Use
`uv run qos new my_ema --dir ~/src` to put it somewhere else.

These tests use generated (synthetic) bars. They show that the strategy runs. They are not a
market-data backtest.

A market-data backtest still needs a local NautilusTrader catalog, usually built from your IBKR
paper account with `scripts/build_catalog.py`. An empty catalog fails with `no bars in catalog`.
If you run IB Gateway or TWS yourself, pass its paper API port (IB Gateway **4002**, TWS
**7497**), e.g. `--port 4002`, or `export IB_PORT=4002`. Without either, and without a gateway
signed in through the Terminal, the script tries **4004** (the Terminal's gateway container).
`IB_PORT` set only in `~/qos/.env` is not read by `build_catalog.py` today. See README, Market data.

Never run the test suite on a server while nodes are trading.

## How to run the tests

```bash
uv run pytest -q
```

CI also checks out [qos-examples](https://github.com/qos-one/qos-examples) and sets `QOS_EXAMPLES`.
You can do the same locally if you have a sibling clone:

```bash
QOS_EXAMPLES=../qos-examples uv run pytest -q
```

Some tests are skipped when there is no data catalog or IB Gateway. That is expected.

## Code style

This repository does **not** currently run Ruff, Black, or a type checker in CI. CI on `main` and
on pull requests is: `uv sync --frozen`, `python -m compileall` of the engine modules, and
`uv run pytest` (see `.github/workflows/ci.yml`).

Please:

- Follow [`.editorconfig`](.editorconfig): UTF-8, LF endings, a newline at end of file, 4-space
  indent for Python, 2-space indent for YAML/JSON.
- Match the style of the file you are changing.
- Keep user-facing text in English.
- Leave existing behaviour alone unless the issue says otherwise — especially anything that can
  place, size or cancel orders. Changes to risk checks need a test that shows the limit still holds.

## Pull requests

1. Branch from `main` and keep the change focused on one thing.
2. Add or update tests; `uv run pytest -q` must pass.
3. Describe what changed, why, and how you tested it (including whether you used synthetic bars,
   a local catalog, or a paper session).
4. Sign every commit (DCO, below). Pull requests without sign-off cannot be merged.

## Never commit

- `.env`, API keys, passwords, tokens or IBKR credentials
- account numbers, personal paths (`/Users/<name>`), e-mail addresses
- `data/`, `reports/`, catalogs, trade history, soak results or other generated files
- strategy packages you created with `qos new` inside this checkout

If you commit a secret by accident, tell us right away and rotate it — removing it in a later
commit is not enough.

## Sign your work (DCO)

We use the [Developer Certificate of Origin](https://developercertificate.org/). Add a sign-off to
every commit to certify that you wrote the change or have the right to submit it:

```bash
git commit -s -m "Fix walk-forward fold boundaries"
```

This adds `Signed-off-by: Your Name <you@example.com>`.

## Licensing of contributions

Contributions are accepted under the license of the files they change:

- **AGPL-3.0-only** for the engine, API, CLI and tools (see `LICENSE`)
- **Apache-2.0** for the developer kit: `qos/sdk/`, `qos/templates/`, `qos_sdk/` and `qos/paths.py`
  (see `NOTICE`)

`NOTICE` also describes additional permissions under AGPL-3.0 section 7 (including a QOS Agent
Exception). Those paragraphs are still marked **DRAFT, for legal review**. Do not treat them as
final, and do not tell users they already apply, until `NOTICE` no longer says DRAFT.

The maintainers may later ask contributors to sign a Contributor License Agreement for larger
changes; until then, no rights beyond these licenses are requested.

The QOS name and logo are trademarks. Forks must use their own name. See `TRADEMARKS.md`.

## Security

Do not open public issues for security problems, including a way to bypass a risk check.

E-mail **seksin.cheevirot@gmail.com** with what you found, where (file, endpoint or version),
steps to reproduce, the impact you expect, and whether you want to be credited. You can also use
the repository's Security tab if private vulnerability reporting is enabled. We aim to acknowledge
reports within 3 working days.

See [SECURITY.md](SECURITY.md).

## Conduct

Be respectful and constructive. Harassment, discrimination and personal attacks are not tolerated
in issues, pull requests or any project space. Maintainers may remove content or block participants
who break this rule.
