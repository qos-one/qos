# Moving the engine and IB Gateway to a Linux VPS

The bot API, the Nautilus nodes, Redis and IB Gateway run on the server. The Terminal stays on your Mac and connects through an SSH tunnel; no trading port is opened on the internet.

| On the server | Path |
|---|---|
| Code (this repo, copied by `deploy-engine.sh`) | `/srv/qos/engine/bot` |
| Data, configs, strategies, models, IB Gateway profiles, `.env` (`QOS_HOME`) | `/srv/qos/home` |
| Services (systemd user units of `qos`) | `qos-api.service`, `qos-catalog.timer`, `qos-watchdog.timer` |
| Redis (Docker, 127.0.0.1:6379) | `bot/docker-compose.yml` |
| IB Gateway (Docker, 127.0.0.1:4003/4004, VNC 127.0.0.1:5901) | `/srv/qos/home/ibgw` |

`ENGINE` below means `qos@<server-ip>`.

## Phase 1 — prepare (the Mac keeps trading; nothing here touches it)

1. New VPS only: base setup from the cloud repo (Docker, firewall, user `qos`), see `cloud/deploy/README.md` steps 1–2.
2. Engine setup (once):
   ```
   scp deploy/engine-setup.sh root@<server-ip>:
   ssh root@<server-ip> 'bash engine-setup.sh'
   ```
3. Copy the code, install packages and services, run the tests on the server:
   ```
   TESTS=1 QOS_ENGINE_HOST=ENGINE deploy/deploy-engine.sh
   ```
4. Operators: in the Terminal on the Mac, Risk → Security, create an admin with a PIN (and an `assistant` operator if you use MCP). They move to the server with the data.

## Phase 2 — cut-over (weekend, FX closed; about 1–2 hours)

Starting the nodes on the server resets their soak count. Nodes start flat: stopping a node does not close its position, and a
restarted node does not take it over (see [After a node restart](#after-a-node-restart-align-the-books)).

1. Mac, Terminal: note open positions (Positions tab / Reconcile), then stop every node. Their open positions stay at IBKR;
   close them first if you do not want to carry them as manual positions.
2. Mac: stop the local services (they would fight the server for the IB session):
   ```
   launchctl list | grep -E "qos|quantx"
   launchctl bootout gui/$(id -u)/<prefix>-api
   launchctl bootout gui/$(id -u)/<prefix>-catalog
   ```
   using the labels the list shows (`<prefix>` is `com.qos` when installed with `scripts/install_launchd.sh`), plus the watchdog if listed.
3. Mac: stop IB Gateway (one IBKR user can only be logged in once; Redis keeps running):
   ```
   cd ~/trading/ibgw && docker compose down
   ```
4. Copy the data (also `.env` and the IB Gateway login, over SSH to your own server):
   ```
   cd ~/trading/bot
   QOS_ENGINE_HOST=ENGINE deploy/migrate-data.sh
   ```
   Every node is set to stopped on the server; nothing trades until you start it.
5. Server: start IB Gateway, approve the login in IBKR Mobile, check the log:
   ```
   ssh ENGINE 'cd /srv/qos/home/ibgw && docker compose up -d && sleep 60 && docker compose logs --tail 30'
   ```
6. Server: start the services and check the IB connection:
   ```
   ssh ENGINE 'systemctl --user enable --now qos-api.service qos-catalog.timer qos-watchdog.timer && sleep 20 && curl -s 127.0.0.1:8000/health'
   ```
   Look for `"ib_connected": true`.
7. Mac: open the tunnel (keep it running), then open the Terminal and sign in with the operator PIN:
   ```
   QOS_ENGINE_HOST=ENGINE deploy/tunnel.sh
   ```
   Check Reconcile matches IBKR, then start the nodes, then align the books (next section).

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

## Day to day

- IB Gateway restarts itself every night (`AUTO_RESTART_TIME`). About once a week IBKR asks for a new login: approve it in IBKR Mobile, or nodes stop trading.
- See the IB Gateway screen: tunnel open, then Finder → Go → Connect to Server → `vnc://localhost:5901`.
- Update the code: `RESTART=1 QOS_ENGINE_HOST=ENGINE deploy/deploy-engine.sh` (nodes keep running; the API adopts them after the restart).
- Logs: `ssh ENGINE 'journalctl --user -u qos-api -n 100 --no-pager'` · node logs in `/srv/qos/home/data/logs`.
- Backup (cron on the server, daily 03:41, keeps 14 days):
  ```
  ssh ENGINE '(crontab -l 2>/dev/null; echo "41 3 * * * tar czf /srv/qos/backups/engine-\$(date +\%F).tgz -C /srv/qos/home data configs strategies models .env && find /srv/qos/backups -name \"engine-*.tgz\" -mtime +14 -delete") | crontab -'
  ```
- Never run the test suite on the server while nodes run (`deploy-engine.sh` refuses `TESTS=1` when the API is up).

## Rollback to the Mac

1. Server: stop nodes in the Terminal, then `ssh ENGINE 'systemctl --user disable --now qos-api.service qos-catalog.timer qos-watchdog.timer; cd /srv/qos/home/ibgw && docker compose down'`.
2. If the server traded, copy back the trade history: `scp ENGINE:/srv/qos/home/data/history.db ~/trading/bot/data/history.db` (and `risk.json`, `audit.jsonl`).
3. Mac: `cd ~/trading/ibgw && docker compose up -d`, then `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<prefix>-api.plist` (and catalog), then start the nodes.
