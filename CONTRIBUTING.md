# Contributing to QOS

Thank you for helping build the open-source Quant Operating System. This repository holds the
engine, the local API used by the QOS Terminal, the `qos` command-line tool and the developer kit.
The Terminal itself lives in the `qos-terminal` repository.

## Ways to help

- **Report a bug** — open an issue with what you did, what you expected, what happened, your OS,
  Python version and the QOS version (`qos --version`). Remove account numbers and credentials
  from logs first.
- **Fix a bug or improve docs** — small pull requests are reviewed fastest.
- **Propose a feature** — open an issue first so we can agree on the design before you write code.
- **Share an agent** — open-source examples are welcome in `qos-examples`. To sell an
  agent, publish it to the qos.one Marketplace instead (see the Developer Agreement).

## Set up

```
git clone <repository>
cd bot
uv sync
uv run pytest -q
```

Live trading is never needed for development. Use an IBKR paper account, synthetic bars
(`qos.sdk.testing.synthetic_bars`) or your own data catalog.

## Pull requests

1. Branch from `main` and keep the change focused on one thing.
2. Add or update tests; `uv run pytest -q` must pass.
3. Keep existing behaviour unless the issue says otherwise — especially anything that can place,
   size or cancel orders. Changes to risk checks need a test that shows the limit still holds.
4. User-facing text is in English.
5. Describe what changed, why, and how you tested it.

## Never commit

- `.env`, API keys, passwords, tokens or IBKR credentials
- account numbers, personal paths (`/Users/<name>`), e-mail addresses
- `data/`, `reports/`, catalogs, trade history, soak results or other generated files

If you commit a secret by accident, tell us right away and rotate it — removing it in a later commit
is not enough.

## Sign your work (DCO)

We use the [Developer Certificate of Origin](https://developercertificate.org/). Add a sign-off to
every commit to certify that you wrote the change or have the right to submit it:

```
git commit -s -m "Fix walk-forward fold boundaries"
```

This adds `Signed-off-by: Your Name <you@example.com>`. Pull requests without sign-off cannot be
merged.

## Licensing of contributions

Contributions are accepted under the license of the files they change: AGPL-3.0-only for the
engine and tools, Apache-2.0 for `qos/sdk/`, `qos/templates/` and `qos_sdk/` (see `NOTICE`).
Contributions to AGPL-licensed files are also covered by the additional permissions in `NOTICE`
(if any), so that those permissions stay valid for the whole project.
The maintainers may later ask contributors to sign a Contributor License Agreement for larger
changes; until then, no rights beyond these licenses are requested.

## Security

Do not open public issues for security problems. E-mail **seksin.cheevirot@gmail.com** with details and a
way to reproduce. We will acknowledge within 3 working days.

## Conduct

Be respectful and constructive. Harassment, discrimination and personal attacks are not
tolerated in issues, pull requests or any project space. Maintainers may remove content or block
participants who break this rule.

## Trademarks

The code is open source; the QOS name and logo are not. See `TRADEMARKS.md`.
