# AGENTS.md

Rules for AI coding agents working in the QOS repositories (`qos-one/qos`, `qos-one/qos-terminal`,
`qos-one/qos-examples`). Claude Code reads this file through `CLAUDE.md`; Cursor reads it directly.
Human contributors should read `CONTRIBUTING.md` first. Where the two disagree, `CONTRIBUTING.md` wins.

## Team

| Who | Role |
|---|---|
| Maintainer (product manager) | Sets priorities and scope, reviews and merges every PR, tags releases, deploys. |
| Claude (Claude Code) | Engineer and reviewer. Opens PRs on `claude/*` branches. |
| Grok (Cursor) | Engineer and reviewer. Opens PRs on `cursor/*` branches (Cursor's default naming). |

- Work only on an issue the maintainer has assigned to you (label `agent:claude` or `agent:grok`).
  If the work you find is bigger than the issue, stop and comment on the issue. Do not widen the PR.
- One agent per issue and one open PR per set of files. If another agent's open PR touches the
  files you need, comment on that PR and wait. Do not build on an unmerged branch unless the issue says so.
- Every PR is reviewed by the other agent before the maintainer merges it. Review the diff, not the
  description, and say what you checked (see "Reviewing").
- Agents never merge, tag, release, push to `main`, change repository settings, or delete branches.

## Hard rules

1. Never ask for, read, print or commit secrets: `.env`, passwords, PINs, API keys, tokens, private
   keys, IBKR credentials. If a task seems to need one, stop and ask the maintainer.
2. Never commit account numbers, personal paths (`/Users/<name>`, `/home/<name>`), personal e-mail
   addresses, server IP addresses, `data/`, `reports/`, `target/`, catalogs, trade history, soak
   results or other generated files. The Privacy check CI job blocks these.
3. Agents do not place, modify or cancel orders, close positions, start or stop strategy nodes,
   restart services, or connect to the maintainer's servers or broker accounts. Use synthetic bars
   (`qos.sdk.testing.synthetic_bars`) or an IBKR paper account you run yourself.
4. Keep existing behaviour unless the issue says otherwise, especially anything that places, sizes or
   cancels orders, the risk gate, the assistant limits, authentication and the AI control modes.
   A change to any of these needs a test that shows the limit or check still holds.
5. Do not change the parity of existing strategies (signals, sizing, fills) without an issue that asks for it.
6. User-facing text, code, comments, docs and commit messages are in English. No Thai text in these repositories.
7. Run the tests before you open or update a PR, and paste the result in the PR.
8. Sign every commit (`git commit -s`), as required by `CONTRIBUTING.md`, and keep the attribution
   lines your tool adds to commit messages.
9. Anything that needs an external sign-up or account (bots, payment, hosting, AI providers): prepare
   the code and docs, do not create or connect the account.
10. Screenshots in PRs, issues or docs must blur IBKR account numbers and personal paths.

## Wording rules (legal and marketing)

- Never say "paper only" about AI. Say: "place orders (paper by default, live only if an admin turns it on)".
- Every place that mentions MCP or AI (README sections, docs pages, Terminal UI, release notes)
  carries this notice, word for word:

  > Optional and experimental. AI can make mistakes; you are responsible for every order it places. Account data you share goes to your chosen AI provider. Keep your token secret. Software, not financial advice.

- No performance figures (returns, win rates, profit, Sharpe) and no "Agents that trade for you" in
  READMEs, docs or release notes, unless clearly labelled "example" or "simulated".
- Do not edit `LICENSE`, `NOTICE`, `TRADEMARKS.md`, `SECURITY.md` or legal pages unless the issue
  says so. These are signed off by the legal reviewer and are replaced exactly as delivered.

## Repositories

| Repository | What | Test command |
|---|---|---|
| `qos-one/qos` | Engine, local API, MCP server, `qos` CLI, SDK | `uv sync --frozen && uv run pytest` |
| `qos-one/qos-terminal` | Terminal (Rust, Dioxus 0.7), desktop and web | `cargo check --locked --no-default-features --features server && cargo check --locked` |
| `qos-one/qos-examples` | Example strategies used by the engine tests | run the engine tests with `QOS_EXAMPLES` pointing here |

- Changes that need both the engine and the Terminal are two PRs, one per repository. Link them to
  each other in the description with "Pairs with …". They are merged and released together.
- `qos-terminal` uses Dioxus 0.7: `cx`, `Scope` and `use_state` no longer exist. Use `use_signal`,
  `use_memo`, `use_resource` and `#[component]` functions.
- Colours: green `#2ee57a`, red `#ff4d5e`.

## Branches, commits and PRs

- Branch from the latest `main`: `claude/<short-topic>` for Claude; Grok keeps Cursor's `cursor/<short-topic>-<id>`.
- Small commits with clear messages in the imperative ("Require the local token before bootstrap").
- PR title says what changes for the user. Fill in every section of the PR template, including
  "Risk" and "How I tested it".
- Keep a PR focused on one issue. Refactors, formatting and dependency bumps go in their own PR.

## Reviewing

When you review the other agent's PR:

1. Check out the branch and run the tests yourself. Do not trust the pasted result alone.
2. Read every changed line. Look first for: authentication or authorisation that can be skipped,
   requests from the AI (`X-QOS-Client: mcp`) that could get more than the `assistant` role, order
   and risk paths, secrets or personal data in the diff, and behaviour changes outside the issue.
3. For an auth or network change, check each client: desktop app (may send no `Origin`), web
   Terminal behind a reverse proxy, MCP clients, and a machine with no operators configured.
4. Post findings as review comments with a concrete failure scenario. Approve only when nothing
   blocking is left. Say what you did not check.

## Handoff

When you stop work on an issue, unfinished or done, leave a comment on the issue:

```
Status: done | blocked | handing over
Branch / PR:
What changed:
Tests: command and result
Not done or not checked:
Next step:
```
