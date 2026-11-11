# Running QOS on a server: operations

A fresh install is one script, see [docs/SELF_HOSTING.md](../docs/SELF_HOSTING.md). This page is for running it day to
day: updates, IB Gateway, the books after a node restart, the web Terminal and remote AI access.

The bot API, the strategy nodes, Redis and IB Gateway run on the server and listen on 127.0.0.1 only. You open the
Terminal in a browser on your own domain, or through an SSH tunnel.

| On the server | Path |
|---|---|
| Code | `/srv/qos/engine/bot` |
| Data, configs, strategies, models, IB Gateway profiles, `.env` (`QOS_HOME`) | `/srv/qos/home` |
| Services (systemd user units of `qos`) | `qos-api`, `qos-catalog.timer`, `qos-watchdog.timer`, `qos-terminal`, `qos-mcp` |
| Redis (Docker, 127.0.0.1:6379) | `docker-compose.yml` in the code folder |
| IB Gateway (Docker, 127.0.0.1:4003/4004, VNC 127.0.0.1:5901) | `/srv/qos/home/ibgw` |

`ENGINE` below means `qos@<server-ip>`. Commands that start with `deploy/` run on your computer, in a clone of this
repository.

## Day to day

| Task | How |
|---|---|
| Update the code | `RESTART=1 QOS_ENGINE_HOST=ENGINE deploy/deploy-engine.sh` (nodes keep running; the API adopts them after the restart) |
| API log | `ssh ENGINE 'journalctl --user -u qos-api -n 100 --no-pager'` |
| Node logs | `/srv/qos/home/data/logs` |
| Open the Terminal through SSH | `QOS_ENGINE_HOST=ENGINE deploy/tunnel.sh`, then point the Terminal at `http://127.0.0.1:8000` |
| See the IB Gateway screen | open the tunnel, then connect any VNC viewer to `localhost:5901` |

- Back up every day (cron on the server, 03:41, keeps 14 days):
  ```
  ssh ENGINE '(crontab -l 2>/dev/null; echo "41 3 * * * tar czf /srv/qos/backups/engine-\$(date +\%F).tgz -C /srv/qos/home data configs strategies models .env && find /srv/qos/backups -name \"engine-*.tgz\" -mtime +14 -delete") | crontab -'
  ```
- Never run the test suite on the server while nodes trade (`deploy-engine.sh` refuses `TESTS=1` while the API is up).
- The hourly catalog update skips while FX is closed once the catalog holds bars up to the Friday close, and gives up
  after 20 minutes instead of hanging.

## IB Gateway

- It restarts itself once a day. Set the time in `/srv/qos/home/ibgw/docker-compose.yml`:
  `AUTO_RESTART_TIME: "05:05 PM"` with `TIME_ZONE: America/New_York`, five minutes after the FX day rolls over, when
  almost nothing trades. Apply it with `docker compose up -d` in that folder (the gateway is down for a minute or two;
  nodes reconnect by themselves).
- About once a week IBKR asks for a fresh login: approve it in IBKR Mobile, or the nodes stop trading.
- If the gateway is still not logged in 10 minutes after a restart (usually stuck at a dialog), `qos-watchdog` runs
  `docker compose restart` in `/srv/qos/home/ibgw`, and once more at 25 minutes. The Terminal warns at 15 minutes.
  By hand: `ssh ENGINE 'cd /srv/qos/home/ibgw && docker compose restart'`.

## After a node restart: align the books

A Nautilus node starts flat every time it starts (cut-over, server reboot, manual stop/start). Its fill-history book still holds
the old position, and closing that position by hand books the close to the manual/agent book. Reconcile then shows
"node live position and fill history disagree" or an `untracked` row.

1. Decide what happens to each old position: close it in the Terminal (Positions → Close), or keep it as a manual position.
2. Start the nodes and wait until each one has a fresh heartbeat (Nodes tab: running).
3. Dry run with the instruments that disagree (nothing is written, nothing is sent to IBKR):
   ```
   ssh ENGINE 'cd /srv/qos/engine/bot && QOS_HOME=/srv/qos/home ~/.local/bin/uv run --frozen scripts/reset_node_books.py FX:USDJPY FX:AUDUSD'
   ```
   Each line shows `book  current → target`: every running node's book is set to the quantity the node reports, the
   manual/agent book to the IBKR quantity minus what the nodes hold. A line `skip: node … has no fresh heartbeat` means that
   node is not running yet: start it and run again.
4. If the targets are right, write them (adds `adjustment` rows with zero P&L; trade history stays intact):
   ```
   ssh ENGINE 'cd /srv/qos/engine/bot && QOS_HOME=/srv/qos/home ~/.local/bin/uv run --frozen scripts/reset_node_books.py FX:USDJPY FX:AUDUSD --apply'
   ```
5. Within 30 s Reconcile shows `ok` (a small residue such as `IBKR +3 · manual/agent +3` is `external` and fine).

Restarting only the API (`RESTART=1` deploy) does not need this: the nodes keep running and are adopted.
USDxxx pairs (USDJPY …): IBKR's virtual position resets at every IB Gateway restart; the API uses the history books and the
cash balance instead, so the nightly restart needs no action.

## Web Terminal (browser, on the engine server)

`deploy/install.sh` sets this up when you give it a domain (see [docs/SELF_HOSTING.md](../docs/SELF_HOSTING.md)).
Several browsers can use it at once; they all talk to the same API.

```
browser ──HTTPS──► Caddy :443 ──► Terminal (web) 127.0.0.1:8080 ──► bot API 127.0.0.1:8000
                        └── /ws ─────────────────────────────────────► bot API (live stream)
```

With `QOS_PUBLIC_TERMINAL=1` the API refuses everything until an operator exists, never creates the first admin from the
web, asks for PINs of 8+ characters, locks sign-in after 20 failures in 10 minutes (5 per operator) and needs a signed-in
operator for the live stream. Each browser keeps its own session in an HttpOnly cookie. Close / Reverse / Close all are
refused while a close order for the same position is still working (two screens, double clicks).

Adding it to an existing server:

1. DNS: an A record `terminal` → the server's IP. Wait until `dig +short terminal.<domain>` shows it.
2. First admin, on the server (the PIN is typed there, never sent anywhere):
   ```
   ssh -t ENGINE 'cd /srv/qos/engine/bot && QOS_HOME=/srv/qos/home ~/.local/bin/uv run --frozen python -m qos.cli operator add <name> --role admin'
   ```
3. API settings: add to `/srv/qos/deploy.env`, then restart the API (nodes keep running):
   ```
   QOS_PUBLIC_TERMINAL=1
   QOS_TRUSTED_ORIGINS=https://terminal.<domain>
   ```
   `ssh ENGINE 'systemctl --user restart qos-api.service'`
4. Caddy and firewall, as root, once (`QOS_ALLOW_IPS` is optional: only these addresses may open the Terminal):
   ```
   ssh root@<server-ip> 'cd /srv/qos/engine/bot/deploy && QOS_TERMINAL_DOMAIN=terminal.<domain> ACME_EMAIL=<you@domain> bash terminal-setup.sh'
   ```
5. Install a release of the web Terminal from [qos-terminal releases](https://github.com/qos-one/qos-terminal/releases):
   ```
   gh release download -R qos-one/qos-terminal -p 'qos-terminal-web.tgz' -D /tmp/qos-terminal-web
   QOS_ENGINE_HOST=ENGINE deploy/deploy-terminal.sh /tmp/qos-terminal-web/qos-terminal-web.tgz
   ```
6. Open `https://terminal.<domain>` and sign in with the operator PIN.

Updates: repeat step 5 with a newer release (the last three are kept in `/srv/qos/terminal/releases`).
Logs: `ssh ENGINE 'journalctl --user -u qos-terminal -n 50 --no-pager'` · `/var/log/caddy/terminal.log`.
Switch it off: `ssh ENGINE 'systemctl --user disable --now qos-terminal.service'` and `systemctl disable --now caddy` (root).

## Remote AI (MCP): Grok, ChatGPT and other AI clients

> Optional and experimental. AI can make mistakes; you are responsible for every order it places. Account data you share goes to your chosen AI provider. Keep your token secret. Software, not financial advice.

Opens the QOS MCP server at `https://<domain>/mcp` so an AI client running elsewhere can read the account and place
orders (paper by default, live only if an admin turns it on). The AI signs in as operator `remote_ai` (role
`assistant`). The API enforces the limits whatever the AI is told: the assistant limits in the Terminal (order size,
orders per hour, no shorts), the risk gate, and Manual mode for orders. Every order is in the audit log and pops up in
the Terminal. On a personal machine with no operators, a caller is that assistant unless it sends the local
token (`X-QOS-Local-Token`) from `data/local_token` (mode 0600, created at install and on the API's first start).
The Terminal and the `qos` CLI read that file; the MCP server does not. `X-Source` and `User-Agent` do not grant
admin. Once an operator exists, sign-in is the operator PIN and this token is not a substitute. With
`QOS_PUBLIC_TERMINAL=1` an operator is required either way. A Terminal that does not send the token yet stays
read-only until it is updated, or until you create an operator.

You choose what the AI may do in the Terminal, top bar → **AI**:

| Mode | What the AI can do |
|---|---|
| Read-only (default) | Read the account. It cannot place orders. |
| Ask first | Prepare orders. Each one waits for your approval in the Terminal (5 minutes), then is sent or dropped. |
| Auto | Confirm its own orders without asking you. |

Closing a position and cancelling an order only reduce exposure, so in Ask first and Auto they are done at once.

1. DNS: an A record such as `mcp.example.com` → the engine server.
2. On the server, once (root):
   ```
   ssh ENGINE
   sudo QOS_MCP_DOMAIN=mcp.example.com bash /srv/qos/engine/bot/deploy/mcp-setup.sh
   ```
   It generates the bearer token and the operator PIN into `/srv/qos/home/.env` (nothing printed), starts `qos-mcp`
   (127.0.0.1:8765) and adds the Caddy site. The AI starts in Read-only: nothing is sent until you choose Ask first
   or Auto in the Terminal.
3. Check from your computer (no order is sent):
   ```
   export QOS_MCP_URL=https://mcp.example.com/mcp
   export QOS_MCP_TOKEN=$(ssh ENGINE "grep '^MCP_BEARER_TOKEN=' /srv/qos/home/.env | cut -d= -f2")
   uv run python scripts/check_remote_mcp.py
   ```
4. AI client: server URL `https://mcp.example.com/mcp`, header `Authorization: Bearer <token>`. xAI API:
   `tools=[{"type": "mcp", "server_url": "https://mcp.example.com/mcp", "server_label": "qos", "authorization": TOKEN}]`.
   Keep the token in the client's secret settings; never paste it into a chat.

Stop it: `systemctl --user disable --now qos-mcp` (as qos). New token: delete the `MCP_BEARER_TOKEN` line in
`/srv/qos/home/.env`, run `mcp-setup.sh` again, update the AI client. Read-only for a while: turn off trading for the
AI assistant in the Terminal. Updates: run `sudo bash deploy/install.sh` again, or `RESTART=1 deploy/deploy-engine.sh`. Both restart `qos-mcp` when it is running.
