# Running QOS on your own server

This guide puts the QOS engine (API, strategy nodes, Redis, IB Gateway) on a Linux server you control, so strategies
keep trading when your computer is off. You choose how to open the Terminal:

| | A. Web Terminal on your domain (recommended) | B. SSH tunnel only |
|---|---|---|
| Open the Terminal | any browser, `https://terminal.<your-domain>` | the Terminal built from source on your computer, through `ssh -L` |
| Open ports | SSH, 80, 443 | SSH only |
| You need | an SSH key for the server and a domain | an SSH key for the server |

Start with **paper trading**. QOS is software, not advice: you are responsible for every order it sends.

## What you need

- A VPS with Ubuntu 24.04 or newer, 4 CPU cores and 8 GB RAM (2 cores / 4 GB runs one or two nodes), 40 GB disk.
- SSH access with a key (`ssh-copy-id you@server`). Turn password logins off once the key works.
- An Interactive Brokers account; a paper account for testing.
- Option A: a domain, and a DNS **A record** such as `terminal.example.com` → the server's IP. Check it with
  `dig +short terminal.example.com` before installing.

## Install

On the server:

```bash
curl -fsSLO https://raw.githubusercontent.com/qos-one/qos/main/deploy/install.sh
less install.sh                     # read it first: it runs as root
```

Option A:

```bash
sudo QOS_TERMINAL_DOMAIN=terminal.example.com ACME_EMAIL=you@example.com bash install.sh
```

Option B:

```bash
sudo bash install.sh
```

Add `QOS_ALLOW_IPS="203.0.113.7/32"` to let only your own addresses open the web Terminal.

The script installs Docker, git, a C compiler and uv; creates the user `qos`; puts the code in `/srv/qos/engine/bot`
and your data in `/srv/qos/home`; starts Redis and the services (`qos-api`, `qos-catalog.timer`,
`qos-watchdog.timer`, and `qos-terminal` for option A); turns on the firewall (SSH, plus 80/443 for option A); and,
for option A, downloads the web Terminal from the
[qos-terminal releases](https://github.com/qos-one/qos-terminal/releases) (checksum verified) and sets up Caddy for
HTTPS. At the end it asks for the first admin name and a PIN (8+ characters for option A). The PIN is typed on the
server and never leaves it.

## First sign-in

1. Open the Terminal:
   - A: open `https://terminal.example.com`.
   - B: on your computer, keep a tunnel open and start the Terminal you built from source (see the
     [qos-terminal README](https://github.com/qos-one/qos-terminal)):
     ```bash
     ssh -N -L 8000:127.0.0.1:8000 -L 5901:127.0.0.1:5901 you@server
     ```
2. Sign in with the admin PIN.
3. Step **IBKR gateway**: enter your IBKR username and password and choose *paper*. QOS starts an IB Gateway
   container on the server ([gnzsnz/ib-gateway](https://github.com/gnzsnz/ib-gateway-docker), a third-party image)
   with your login stored in `/srv/qos/home/ibgw` (readable by `qos` only). Approve the login in IBKR Mobile if asked.
4. Step **Account**: pick the account. Check the Reconcile tab, then start a strategy node.

To see the IB Gateway screen (rare: a login prompt that needs a click), open the tunnel from option B and connect a VNC
viewer to `localhost:5901`.

## Day to day

| Task | Command (on the server) |
|---|---|
| Update QOS | `sudo bash install.sh` again (with the same settings). Running nodes keep trading. |
| API log | `sudo -u qos XDG_RUNTIME_DIR=/run/user/$(id -u qos) journalctl --user -u qos-api -n 100` |
| Node logs | `/srv/qos/home/data/logs` |
| Add an operator | sign in as admin → Risk → Security, or `qos operator add` (see below) |
| Reset a PIN | `sudo -u qos bash -c 'cd /srv/qos/engine/bot && QOS_HOME=/srv/qos/home ~/.local/bin/uv run --frozen python -m qos.cli operator pin <id>'` |
| Back up | `/srv/qos/home` holds everything (data, configs, strategies, `.env`): copy it while the API is stopped |

- IB Gateway restarts once a day at 17:05 New York, just after the FX day rolls over, and IBKR asks for a fresh login
  about once a week (approve it in IBKR Mobile). If the gateway is still logged out 10 minutes after a restart, the
  watchdog restarts it; the Terminal warns after 15 minutes.
- Stopping a node cancels its orders and, while the market is open, closes the positions it opened. With FX closed
  (Friday 17:00 to Sunday 17:00 New York) positions are kept and strategies send no orders.
- After a node restarts it starts flat and does not know older positions; the "After a node restart" section of
  [deploy/README.md](../deploy/README.md) shows how to line up the books.
- Never run the test suite on the server while nodes trade.

## Remote AI (optional)

> Optional and experimental. AI can make mistakes; you are responsible for every order it places. Account data you share goes to your chosen AI provider. Keep your token secret. Software, not financial advice.

To let an AI client such as Grok (xAI API) or ChatGPT read the account and place orders (paper by default, live only
if an admin turns it on), open the MCP server on its own domain:
`sudo QOS_MCP_DOMAIN=mcp.example.com bash deploy/mcp-setup.sh`. The AI acts as an operator with role `assistant`: the
assistant limits and the risk gate always apply. On a personal machine with no operators, a caller is that assistant
unless it sends the local token from `data/local_token` (see Security notes). The MCP server is not given that token.
`X-Source` and `User-Agent` do not grant admin.

By default the AI cannot place orders. In the Terminal, top bar → **AI**, choose **Ask first** to approve each of its
orders yourself, or **Auto** to let it confirm its own orders without asking you. See the "Remote AI (MCP)" section of
[deploy/README.md](../deploy/README.md).

## Security notes

- Only SSH (and 80/443 for option A) are open. The API, Redis, IB Gateway and VNC listen on 127.0.0.1.
- No operators yet (a personal machine): the API keeps `data/local_token` (mode 0600) under `QOS_HOME`, or in the
  engine data directory when `QOS_HOME` is not set. The Terminal and the `qos` command send it as
  `X-QOS-Local-Token` and are the local admin. Other callers, including the AI, are the assistant. The value is not
  put in `.env`. A Terminal on another computer (the SSH tunnel) needs a copy of that file and `QOS_LOCAL_TOKEN_FILE`
  pointing at the copy. `qos operator add` switches the machine to PIN sign-in; the file then no longer grants admin.
  With `QOS_PUBLIC_TERMINAL=1` an operator is required either way.
- Option A runs with `QOS_PUBLIC_TERMINAL=1`: nothing works until an operator exists, the first admin can only be
  created on the server, PINs are 8+ characters, sign-in locks after repeated failures, every browser has its own
  session (HttpOnly cookie) and the live stream needs a signed-in operator.
- The web Terminal can send orders to your broker. Use a long PIN, keep your domain to yourself, and prefer
  `QOS_ALLOW_IPS` when you use it from fixed places.
- Live trading: the Terminal asks for a separate live confirmation code. Stay on paper until you trust your setup.

## License

QOS is AGPL-3.0-only (the SDK is Apache-2.0). If you change QOS and let other people use it over a network (for
example a web Terminal for your team), you must offer them the source code of your version. Running it for yourself
needs nothing. This is a summary, not legal advice; the [LICENSE](../LICENSE) is what counts.
