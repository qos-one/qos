"""
qos — the QOS (Quant Operating System) command line · installed with `pip install qos-sdk`, or scripts/qos on a machine with QOS

  qos new <name> [--ml] [--dir .]
  qos install <folder | strategy.py | git url[#subdir]> [--force] [--no-eval] [--bars synthetic]
  qos status [--json]
  qos evaluate <name@version>
  qos uninstall <name@version>
  qos data export <instrument> <tf> -o file.parquet [--days 365 | --start … --end …]
  qos data list · qos data pull <dataset> [--version V] · qos data check <dataset> · qos data remove <dataset>
  qos data publish <dataset> <version> --title … [--sku data.x] [--match '*1-MINUTE*']   (admin)
  qos home [show | init <folder>]
  qos backup [--dest DIR] [--with-env] · qos backup list · qos restore <file> [--with-env] [--yes]   (stop all nodes, then the watchdog + API first)
  qos login [--name NAME] [--no-browser] [--force] · qos logout
  qos cloud [status | sync | devices | remove <device_id>] [--json]
  qos publish <folder> [--check] [--title …] [--description …]          (developer account)
  qos agent status [name] · qos agent withdraw name@ver · qos agent queue|approve|reject|recheck|list|unlist|restart … (admin)
  qos feed status · qos feed push [--days 30] [--instrument EURUSD]      (admin: live bars for forward paper)
  qos store list · qos store show <agent> · qos store install <agent> [--version V] · qos store buy <agent> [--plan month]
  qos data buy <dataset> [--plan …] · qos billing [status | portal | upgrade pro [--plan month]]
  qos agent price <name> --monthly 19 | --yearly 190 | --once 99 | --remove month · qos payouts [status | onboard]   (developer)

Install as a command:  alias qos="<bot folder>/scripts/qos"   (or pip install qos-sdk)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from qos.install import DEFAULT_API, InstallError, api_call, install  # noqa: E402

TEMPLATES = Path(__file__).resolve().parent / "templates"



def _fmt_money(v) -> str:
    return "—" if v is None else f"{v:+,.0f}"


def cmd_install(a) -> int:
    try:
        out = install(a.source, force=a.force, evaluate=not a.no_eval, bars=a.bars, days=a.days, catalog=a.catalog, api=a.api or None)
        if a.result_line:
            print("RESULT " + json.dumps({"ok": True, "ref": out["ref"], "path": out["path"]}), flush=True)
        return 0
    except InstallError as e:
        print(f"✕ install failed: {e}", file=sys.stderr)
        return 1


def cmd_status(a) -> int:
    try:
        r = api_call("GET", "/strategies/packages", api=a.api)
    except InstallError as e:
        print(f"✕ {e}", file=sys.stderr)
        return 1
    if r is None:
        print("✕ QOS API is not running", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(r, indent=2, default=str))
        return 0
    print(f"strategies: {r['dir']}")
    rows = [("PACKAGE", "KIND", "STATE", "BACKTEST 90D", "DD", "WALK-FORWARD", "NODES", "NOTE")]
    for p in r["packages"]:
        st = p.get("lifecycle") or {}
        bt, wf = st.get("backtest") or {}, st.get("walkforward") or {}
        if p["status"] != "ok":
            state, note = "error", (p.get("error") or "")[:70]
        else:
            state, note = st.get("state", "—"), ("evaluating…" if st.get("evaluating") else st.get("note") or "")
        if p["kind"] == "builtin" and not p["nodes"]:
            continue
        rows.append((p["ref"], p["kind"], state, _fmt_money(bt.get("pnl_usd")) if bt else "—",
                     f"{bt['max_drawdown_pct']:.1f}%" if bt.get("max_drawdown_pct") is not None else "—",
                     f"{wf.get('positive_folds', 0)}/{wf.get('folds', 0)} {_fmt_money(wf.get('pnl_usd'))}" if wf.get("folds") else (wf.get("error", "—")[:20] if wf else "—"),
                     ",".join(p["nodes"]) or "—", note))
    w = [max(len(str(r_[i])) for r_ in rows[:]) for i in range(7)]
    for r_ in rows:
        print("  ".join(str(c).ljust(w[i]) for i, c in enumerate(r_[:7])) + "  " + r_[7])
    if r.get("queue"):
        print(f"evaluation queue: {', '.join(r['queue'])}")
    return 0


def cmd_evaluate(a) -> int:
    try:
        r = api_call("POST", f"/strategies/packages/{a.ref}/evaluate", api=a.api)
    except InstallError as e:
        print(f"✕ {e}", file=sys.stderr)
        return 1
    if r is None:
        print("✕ QOS API is not running", file=sys.stderr)
        return 1
    print(f"evaluation of {a.ref}: {r.get('status')}")
    return 0


def cmd_uninstall(a) -> int:
    try:
        r = api_call("DELETE", f"/strategies/packages/{a.ref}", api=a.api)
    except InstallError as e:
        print(f"✕ {e}", file=sys.stderr)
        return 1
    if r is None:
        print("✕ QOS API is not running (uninstall needs it to check which nodes use the package)", file=sys.stderr)
        return 1
    print(f"removed {a.ref}")
    return 0


def new_package(name: str, ml: bool = False, parent: str | Path = ".", log=print) -> Path:
    """Create a package from a template (qos new) — tests and qos install work right away"""
    import re
    import string
    import subprocess
    from qos.sdk.strategy import NAME_RE
    if not NAME_RE.match(name):
        raise ValueError(f"name '{name}' must be 2–32 chars: a-z, 0-9, _ (starting with a letter)")
    dest = Path(parent).expanduser().resolve() / name
    if dest.exists() and any(dest.iterdir()):
        raise ValueError(f"{dest} already exists and is not empty")
    cls = "".join(w.title() for w in re.split(r"_+", name) if w)
    values = {"name": name, "Class": cls, "label": name.replace("_", " ").title()}
    src = TEMPLATES / ("ml" if ml else "basic")
    for f in sorted(src.rglob("*")):
        if f.is_dir() or "__pycache__" in f.parts:
            continue
        out = dest / f.relative_to(src)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(string.Template(f.read_text()).safe_substitute(values))
    if ml:
        try:
            import joblib  # noqa: F401
            import sklearn  # noqa: F401
        except ImportError:
            raise ValueError("the ML template needs scikit-learn and joblib — pip install 'qos-sdk[ml]'") from None
        log("→ training the example model on synthetic bars (python train.py --synthetic) …")
        p = subprocess.run([sys.executable, "train.py", "--synthetic"], cwd=dest, capture_output=True, text=True, timeout=300,
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                                "PYTHONPATH": os.pathsep.join(filter(None, [str(Path(__file__).resolve().parents[1]), os.getenv("PYTHONPATH")]))})
        if p.returncode != 0:
            raise ValueError(f"train.py failed: {(p.stderr or p.stdout)[-400:]}")
        log("   " + p.stdout.strip().splitlines()[-1])
    log(f"→ created {dest}")
    for f in sorted(x for x in dest.rglob("*") if x.is_file()):
        log(f"   {f.relative_to(dest)}")
    log(f"next: cd {dest} && pytest tests && qos install .")
    return dest


def cmd_new(a) -> int:
    try:
        new_package(a.name, ml=a.ml, parent=a.dir)
        return 0
    except ValueError as e:
        print(f"✕ {e}", file=sys.stderr)
        return 1


def cmd_data_export(a) -> int:
    import pandas as pd
    from qos.sdk import catalog
    end = a.end or pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d %H:%M")
    start = a.start or (pd.Timestamp(end) - pd.Timedelta(days=a.days)).strftime("%Y-%m-%d %H:%M")
    try:
        info = catalog.export(a.instrument, a.tf, a.output, start, end, a.catalog)
    except (ValueError, OSError) as e:
        print(f"✕ {e}", file=sys.stderr)
        return 1
    print(f"exported {info['bars']:,} bars {info['instrument']} {info['tf_minutes']}m · {info['from'][:16]} → {info['to'][:16]} → {info['path']}")
    return 0


def cmd_home(a) -> int:
    from qos import paths
    if a.home_cmd == "init":
        print(f"→ QOS_HOME {Path(a.folder).expanduser().resolve()}")
        paths.init_home(a.folder)
        print(f"next: export QOS_HOME={Path(a.folder).expanduser().resolve()}  (add it to ~/.zshrc), fill in .env, then start the API")
        return 0
    if a.json:
        print(json.dumps(paths.summary(), indent=2))
        return 0
    for k, v in paths.summary().items():
        print(f"{k:<11} {v}")
    return 0


def _backup_mod():
    try:
        from api import backup
        return backup
    except ImportError:
        print("✕ backup/restore needs the full QOS (bot folder), not the qos-sdk package", file=sys.stderr)
        return None


def cmd_backup(a) -> int:
    b = _backup_mod()
    if b is None:
        return 1
    if a.backup_cmd == "list":
        for x in b.list_backups(a.dest):
            print(f"{x['created'][:16]}  {x['bytes'] / 1e6:7.2f} MB  {'weekly ' if x['weekly'] else '       '}{x['file']}")
        return 0
    r = b.create(a.dest, include_env=a.with_env or None)
    print(f"backup → {r['file']} ({r['files']} files, {r['bytes'] / 1e6:.2f} MB)" + (f" · removed {len(r['removed'])} old" if r["removed"] else ""))
    return 0


def cmd_restore(a) -> int:
    b = _backup_mod()
    if b is None:
        return 1
    try:
        m = b.verify(a.file)
    except (ValueError, OSError, KeyError) as e:
        print(f"✕ {e}", file=sys.stderr)
        return 1
    try:
        running = api_call("GET", "/health", api=a.api, timeout=3) is not None
    except InstallError:
        running = True
    if running:
        print("✕ the QOS API is running. Restore steps:\n"
              "  1. stop every node in the Terminal (Nodes › Stop) — node processes keep running when the API stops\n"
              "  2. stop the watchdog and the API:\n"
              "     launchctl bootout gui/$(id -u)/<prefix>-watchdog\n     launchctl bootout gui/$(id -u)/<prefix>-api\n"
              "  3. qos restore <file> · 4. start the API (restored nodes come back stopped — start them again yourself)", file=sys.stderr)
        return 1
    from qos.install import live_pidfile_nodes
    live = live_pidfile_nodes()
    if live:
        print(f"✕ node(s) still running: {', '.join(live)} — start the API, stop them in the Terminal (Nodes › Stop), "
              "stop the API again, then restore", file=sys.stderr)
        return 1
    print(f"backup from {m['created']} · {len(m['files'])} files · checksums OK")
    if not a.yes and input("restore it over the current data? current files are kept in data/.pre-restore-* [y/N] ").strip().lower() != "y":
        print("cancelled")
        return 1
    try:
        r = b.restore(a.file, include_env=a.with_env)
    except b.RestoreRefused as e:
        print(f"✕ {e}", file=sys.stderr)
        return 1
    print(f"restored {r['restored']} files · nodes come back stopped" + (f" · previous files → {r['previous_saved_to']}" if r["previous_saved_to"] else ""))
    return 0


def _ago(ts) -> str:
    if not ts:
        return "never"
    import time as _t
    s = int(_t.time() - ts)
    return f"{s}s ago" if s < 120 else f"{s // 60}m ago" if s < 7200 else f"{s // 3600}h ago" if s < 172800 else f"{s // 86400}d ago"


def _cloud_local():
    from qos.cloud import client as cc
    return cc.get()


def _cloud(a, method: str, path: str, body: dict | None = None):
    """Through the QOS API if it is running (so the refresh token has a single owner) · otherwise directly on this machine"""
    try:
        return api_call(method, "/cloud" + path, body, api=a.api)
    except InstallError as e:
        raise SystemExit(f"error: {e}")


def _print_status(s: dict) -> None:
    if not s.get("signed_in"):
        print(f"QOS Cloud ({s.get('url')}): not signed in" + (f" — {s['last_error']}" if s.get("last_error") else ""))
        print("sign in with: qos login")
        return
    left = s.get("offline_left_s") or 0
    print(f"QOS Cloud ({s.get('url')}, {s.get('env')}): signed in as {s.get('email')} · plan {s.get('plan') or '—'}")
    print(f"  device {s.get('device_id')} · token store {s.get('token_store')}")
    print(f"  cloud features {'ON' if s.get('cloud_features') else 'OFF'} · works offline for {left // 86400}d {left % 86400 // 3600}h"
          f" · last sync {_ago(s.get('last_sync'))}")
    for g in s.get("grants") or []:
        print(f"  ✓ {g['sku']}" + (f" (until {__import__('datetime').datetime.fromtimestamp(g['exp']):%Y-%m-%d})" if g.get("exp") else ""))
    print(f"  revocations seq {s['revocations']['seq']} ({s['revocations']['count']}) · trusted keys {s['trust']['keys']}"
          + (" (first-use pin, dev)" if s["trust"]["tofu"] else ""))
    if s.get("last_error"):
        print(f"  last error: {s['last_error']}")


def cmd_data_cloud(a) -> int:
    from qos.cloud import data as cd
    from qos.cloud.client import CloudError
    try:
        if a.data_cmd == "list":
            o = cd.overview()
            if a.json:
                print(json.dumps(o, indent=2))
                return 0
            if o["error"]:
                print(f"QOS Cloud: {o['error']} (showing datasets on this computer)")
            if not o["datasets"]:
                print("no datasets yet")
            for d in o["datasets"]:
                loc, lat = d.get("local"), (d.get("latest") or {})
                have = f"have {loc['version']} ({loc['state']})" if loc else "not pulled"
                acc = "" if d.get("entitled") is None else ("" if d["entitled"] else f" · needs {d['sku']}")
                upd = " · update available" if d.get("update") else ""
                size = f" · {lat.get('bytes', 0) / 1e6:,.1f} MB" if lat else ""
                print(f"  {d['slug']:<16} {d.get('title', ''):<28} latest {lat.get('version', '—')}{size} · {have}{acc}{upd}")
                if loc and loc.get("reason"):
                    print(f"  {'':<16} {loc['reason']}")
        elif a.data_cmd == "pull":
            cd.pull(a.dataset, a.version, log=print)
        elif a.data_cmd == "check":
            r = cd.check(a.dataset)
            print(f"{r['slug']}@{r['version']}: {r['files']} files OK")
        elif a.data_cmd == "remove":
            print("removed" if cd.remove(a.dataset) else f"{a.dataset} is not on this computer")
        elif a.data_cmd == "buy":
            ds = next((d for d in cd.remote() if d["slug"] == a.dataset), None)
            if ds is None or not ds.get("sku"):
                print(f"{a.dataset} is not for sale")
                return 1
            return _buy(a, ds["sku"], a.plan)
        elif a.data_cmd == "publish":
            from qos import paths
            cd.publish(a.dataset, a.version, a.catalog or paths.catalog_dir(), a.match or None, title=a.title,
                       sku=a.sku, description=a.description, notes=a.notes, draft=a.draft, log=print)
    except (CloudError, ValueError) as e:
        print(f"error: {getattr(e, 'message', e)}")
        return 1
    return 0


def _eval_line(v: dict) -> str:
    ev = v.get("evaluation") or {}
    bt, wf = ev.get("backtest") or {}, ev.get("walkforward") or {}
    parts = []
    if bt:
        parts.append(f"backtest {bt.get('days')}d pnl {bt.get('pnl_usd', 0):+,.0f} · {bt.get('trades')} trades · DD {bt.get('max_drawdown_pct')}%")
    if wf:
        parts.append(f"walk-forward {wf.get('positive_folds')}/{wf.get('folds')} folds +")
    return " · ".join(parts)


def _report_lines(r: dict) -> list[str]:
    out = []
    for i in (r.get("scan") or {}).get("issues", []):
        out.append(f"{'✕' if i['level'] == 'error' else '!'} {i['file']}:{i['line']} {i['message']}")
    ch = r.get("checks") or {}
    for c in ch.get("checks", []):
        if not c.get("ok"):
            out.append(f"✕ check {c['name']}: {c.get('detail')}")
    if r.get("error"):
        out.append(f"✕ {r['error']}")
    return out


def cmd_publish(a) -> int:
    from qos.cloud import agents as ca
    from qos.cloud.client import CloudError
    try:
        ca.publish(a.folder, a.title, a.description, a.check, a.catalog, log=print, distribution="source" if a.source else "compiled")
    except CloudError as e:
        print(f"error: {e.message}")
        return 1
    return 0


def cmd_agent(a) -> int:
    from qos.cloud import agents as ca
    from qos.cloud.client import CloudError
    try:
        if a.agent_cmd == "status":
            rows = ca.mine()
            if a.json:
                print(json.dumps(rows, indent=2))
                return 0
            if a.ref:
                rows = [r for r in rows if r["slug"] == a.ref.split("@")[0]]
            if not rows:
                print("no agents submitted yet — qos publish <folder>")
            for ag in rows:
                print(f"{ag['slug']}  {ag['title']}")
                for v in ag["versions"]:
                    if a.ref and "@" in a.ref and v["version"] != a.ref.split("@")[1]:
                        continue
                    print(f"  {v['version']:<9} {v['status']:<13} {_eval_line(v)}")
                    fw = v.get("forward")
                    if fw:
                        st = fw.get("stats") or {}
                        line = f"forward paper: {fw['status']}"
                        if st.get("trades") is not None:
                            line += f" · {st.get('days', 0)}d · {st.get('trades')} trades · pnl {st.get('pnl_usd') or 0:+,.0f} · DD {st.get('max_drawdown_pct') or 0}%"
                        if fw.get("reason"):
                            line += f" · {fw['reason']}"
                        print(f"  {'':<9} {line}" + (" · LISTED" if v.get("listed_at") else ""))
                    if v.get("review_notes"):
                        print(f"  {'':<9} reviewer: {v['review_notes']}")
                    for ln in _report_lines(v.get("report") or {})[:15]:
                        print(f"  {'':<9} {ln}")
        elif a.agent_cmd == "queue":
            q = ca.queue(status=a.status)
            if a.json:
                print(json.dumps(q, indent=2))
                return 0
            if not q:
                print(f"nothing {a.status}")
            for v in q:
                print(f"{v['slug']}@{v['version']}  {v['title']}  {_eval_line(v)}")
                for ln in _report_lines(v.get("report") or {})[:10]:
                    print(f"    {ln}")
        elif a.agent_cmd == "price":
            from qos.cloud import billing
            if not a.ref:
                print("usage: qos agent price <name> --monthly 19 | --yearly 190 | --once 99 | --remove month")
                return 2
            name = a.ref.split("@")[0]
            if a.remove:
                billing.remove_agent_price(name, a.remove)
                print(f"{name}: {a.remove} price removed")
            for kind, amt in (("month", a.monthly), ("year", a.yearly), ("one_time", a.once)):
                if amt is not None:
                    r = billing.set_agent_price(name, kind, amt)
                    print(f"{name}: {billing.fmt(r)}")
        elif a.agent_cmd == "build":
            if not a.ref or "@" not in a.ref:
                print("usage: qos agent build name@version [--no-upload] [--out DIR]")
                return 2
            from qos.cloud import build as cb
            cb.build(a.ref, upload=not a.no_upload, out=a.out, log=print)
        else:
            if not a.ref:
                print(f"usage: qos agent {a.agent_cmd} name@version")
                return 2
            v = ca.act(a.ref, a.agent_cmd, a.notes or "")
            print(f"{a.ref}: {v['status']}")
    except CloudError as e:
        print(f"error: {e.message}")
        return 1
    return 0


def cmd_feed(a) -> int:
    from qos.cloud import feed
    from qos.cloud.client import CloudError
    try:
        if a.feed_cmd == "push":
            r = feed.push(instruments=a.instrument or None, days=a.days, log=print)
            print(f"sent {sum(r.values())} new bars")
        else:
            st = feed.status()["instruments"]
            if not st:
                print("no live bars on QOS Cloud yet — qos feed push")
            for i in st:
                print(f"  {i['instrument']:<8} {i['bars']:>8,} bars  {i['first'][:16]} → {i['last'][:16]}")
    except CloudError as e:
        print(f"error: {e.message}")
        return 1
    return 0


def cmd_store(a) -> int:
    from qos.cloud import store
    from qos.cloud.client import CloudError
    try:
        if a.store_cmd == "list":
            o = _cloud(a, "GET", "/store") or store.overview()
            if a.json:
                print(json.dumps(o, indent=2))
                return 0
            if o.get("error"):
                print(f"QOS Cloud: {o['error']} (showing installed agents only)")
            if not o["agents"]:
                print("no agents in the store yet")
            for ag in o["agents"]:
                fw = (ag.get("forward") or {}).get("stats") or {}
                have = f"installed {', '.join(ag['installed'])}" if ag.get("installed") else "not installed"
                flags = (" · update available" if ag.get("update") else "") + (" · WITHDRAWN" if ag.get("revoked") else "") \
                    + (" · no longer listed" if ag.get("unlisted") else "")
                print(f"  {ag['slug']:<18} {ag.get('version', ''):<8} {ag.get('title', '')[:30]:<30} {have}{flags}")
                ev = _eval_line(ag)
                if ev:
                    print(f"  {'':<18} {ev}")
                if fw:
                    print(f"  {'':<18} forward {fw.get('days', 0)}d · {fw.get('trades', 0)} trades · pnl {fw.get('pnl_usd') or 0:+,.0f} · DD {fw.get('max_drawdown_pct') or 0}%")
        elif a.store_cmd == "show":
            d = _cloud(a, "GET", f"/store/{a.agent}") or store.detail(a.agent)
            if a.json:
                print(json.dumps(d, indent=2))
                return 0
            st = d.get("stats") or {}
            print(f"{d['slug']}@{d['version']} · forward {d['status']} since {d['started_at'][:10]}" + (" · listed" if d.get("listed") else ""))
            print(f"  {st.get('days', 0)} days · {st.get('trades', 0)} trades · pnl {st.get('pnl_usd') or 0:+,.2f} · "
                  f"win rate {st.get('win_rate')} · max DD {st.get('max_drawdown_pct') or 0}%")
            for t in (d.get("recent_trades") or [])[-10:]:
                print(f"  {str(t.get('closed', ''))[:16]}  {t.get('side', ''):<5} {t.get('pnl', 0):+,.2f}")
        elif a.store_cmd == "buy":
            if not a.agent:
                print("usage: qos store buy <agent> [--plan month|year|one_time]")
                return 2
            return _buy(a, f"agent.{a.agent}", a.plan)
        elif a.store_cmd == "install":
            if not a.agent:
                print("usage: qos store install <agent> [--version V]")
                return 2
            r = _cloud(a, "POST", f"/store/{a.agent}/install", {"version": a.version})
            if r is None:
                store.install(a.agent, a.version, log=print)
            else:
                print(f"downloaded {r['slug']}@{r['version']} — installing as Strategy lab job {r['job'].get('id')} "
                      f"(checks, then backtest + walk-forward). Follow it with: qos status")
    except CloudError as e:
        print(f"error: {e.message}")
        return 1
    return 0


def _buy(a, sku: str, kind: str | None) -> int:
    import time as _t
    import webbrowser
    from qos.cloud import billing
    pr = billing.pick(billing.prices(sku), kind)
    print(f"{sku}: {billing.fmt(pr)}")
    r = _cloud(a, "POST", "/billing/checkout", {"price_id": pr["id"]})
    if r is None:
        r = billing.checkout(pr["id"])
        webbrowser.open(r["url"])
        print(f"complete the payment in your browser: {r['url']}")
        st = billing.wait_paid(r["purchase_id"])
    else:
        print(f"complete the payment in your browser: {r['url']}")
        st = {"status": "pending"}
        for _ in range(225):
            _t.sleep(4)
            st = _cloud(a, "GET", f"/billing/purchase/{r['purchase_id']}") or st
            if st["status"] in billing.DONE and not st.get("waiting"):
                break
    print(f"{sku}: {st['status']}" + (" — ready to use" if st["status"] in billing.PAID else ""))
    return 0 if st["status"] in billing.PAID else 1


def cmd_billing(a) -> int:
    from qos.cloud import billing
    from qos.cloud.client import CloudError
    try:
        if a.billing_cmd == "portal":
            r = _cloud(a, "POST", "/billing/portal")
            if r is None:
                import webbrowser
                webbrowser.open(billing.portal())
            print("opened billing in your browser (change card, cancel, receipts)")
        elif a.billing_cmd == "upgrade":
            return _buy(a, f"plan.{a.item or 'pro'}", a.plan)
        else:
            r = _cloud(a, "GET", "/billing") or billing.purchases()
            print(f"plan {r['plan']}")
            for p in r["purchases"]:
                per = {"one_time": "one-time", "month": "monthly", "year": "yearly"}[p["kind"]]
                until = f" · until {p['period_end'][:10]}" if p.get("period_end") else ""
                print(f"  {p['sku']:<24} {p['status']:<9} {per:<8} {p['amount_cents'] / 100:,.2f} {p['currency'].upper()}{until}")
    except CloudError as e:
        print(f"error: {e.message}")
        return 1
    return 0


def cmd_payouts(a) -> int:
    import webbrowser
    from qos.cloud import billing
    from qos.cloud.client import CloudError
    try:
        if a.payouts_cmd == "onboard":
            url = billing.onboard()
            webbrowser.open(url)
            print(f"finish payout setup in your browser: {url}")
            return 0
        r = billing.payouts()
        print(f"payouts: {r['status']} · gross {r['gross_cents'] / 100:,.2f} · net {r['net_cents'] / 100:,.2f}")
        for i in r["items"]:
            print(f"  {i['sku']:<24} {i['sales']} sales · {i['buyers']} buyers · net {i['net_cents'] / 100:,.2f} {i['currency'].upper()}"
                  + (f" · {i['refunds']} refunds" if i["refunds"] else ""))
    except CloudError as e:
        print(f"error: {e.message}")
        return 1
    return 0


def cmd_login(a) -> int:
    import time as _t
    import webbrowser
    r = _cloud(a, "POST", "/login", {"device_name": a.name, "force": a.force})
    via_api = r is not None
    if not via_api:
        from qos.cloud.client import CloudError
        c = _cloud_local()
        try:
            r = c.login_start(a.name, a.force)
        except CloudError as e:
            print(f"error: {e.message}")
            return 1
    uri = r.get("verification_uri_complete") or r["verification_uri"]
    print(f"\n  Code: {r['user_code']}\n  Approve at: {uri}\n")
    if not a.no_browser:
        webbrowser.open(uri)
    print("waiting for approval… (Ctrl+C to cancel)")
    try:
        if via_api:
            while True:
                _t.sleep(3)
                st = _cloud(a, "GET", "/login") or {"state": "error", "message": "QOS API stopped"}
                if st["state"] not in ("pending", "slow_down"):
                    break
        else:
            interval, end = r.get("interval", 5), _t.time() + r.get("expires_in", 600)
            st = {"state": "expired"}
            while _t.time() < end:
                _t.sleep(interval)
                st = c.login_poll(r["device_code"])
                if st["state"] == "slow_down":
                    interval += 5
                elif st["state"] != "pending":
                    break
    except KeyboardInterrupt:
        if via_api:
            _cloud(a, "DELETE", "/login")
        print("cancelled")
        return 1
    if st["state"] != "signed_in":
        print(f"not signed in: {st['state']}" + (f" — {st.get('message')}" if st.get("message") else ""))
        return 1
    _print_status(_cloud(a, "GET", "/status") or _cloud_local().status())
    return 0


def cmd_logout(a) -> int:
    s = _cloud(a, "POST", "/logout")
    if s is None:
        _cloud_local().logout()
    print("signed out of QOS Cloud")
    return 0


def cmd_cloud(a) -> int:
    from qos.cloud.client import CloudError
    what = a.cloud_cmd or "status"
    try:
        if what == "status":
            s = _cloud(a, "GET", "/status") or _cloud_local().status()
        elif what == "sync":
            s = _cloud(a, "POST", "/sync") or _cloud_local().sync()
        elif what == "devices":
            s = _cloud(a, "GET", "/devices") or _cloud_local().devices()
        elif what == "remove":
            if not a.device_id:
                print("usage: qos cloud remove <device_id>")
                return 2
            s = _cloud(a, "DELETE", f"/devices/{a.device_id}") or _cloud_local().revoke_device(a.device_id)
        else:
            return 2
    except CloudError as e:
        print(f"error: {e.message}")
        return 1
    if a.json:
        print(json.dumps(s, indent=2))
    elif what in ("status", "sync"):
        _print_status(s)
    elif what == "devices":
        print(f"plan {s['plan']} · {sum(d['counts_to_limit'] for d in s['devices'])}/{s['limit']} devices")
        for d in s["devices"]:
            print(f"  {'*' if d['current'] else ' '} {d['id']}  {d['kind']:<7} {d['name']}  {d['platform']}  last seen {d['last_seen_at'][:16]}")
    else:
        print(f"removed {a.device_id}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="qos", description="QOS — Quant Operating System")
    ap.add_argument("--api", default=DEFAULT_API, help="QOS API url (default %(default)s)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install", help="check and install a strategy package")
    p.add_argument("source", help="folder with strategy.py, a single strategy.py, or a git url (git+https://…#subdir)")
    p.add_argument("--force", action="store_true", help="replace the same version if it is not used by a node")
    p.add_argument("--no-eval", action="store_true", help="do not start backtest + walk-forward after installing")
    p.add_argument("--bars", default="auto", choices=["auto", "catalog", "synthetic"],
                   help="data for the install checks (auto = data catalog if present, else synthetic)")
    p.add_argument("--days", type=int, default=30, help="days of data for the install checks")
    p.add_argument("--catalog", default=None, help="data catalog folder (default bot/data/catalog)")
    p.add_argument("--result-line", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_install)
    p = sub.add_parser("status", help="packages, versions, lifecycle state and latest results")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)
    p = sub.add_parser("evaluate", help="run backtest + walk-forward for an installed package")
    p.add_argument("ref")
    p.set_defaults(fn=cmd_evaluate)
    p = sub.add_parser("uninstall", help="remove a package version (refused while a node uses it)")
    p.add_argument("ref")
    p.set_defaults(fn=cmd_uninstall)
    p = sub.add_parser("new", help="create a strategy package from a template")
    p.add_argument("name")
    p.add_argument("--ml", action="store_true", help="ML template: features.py, train.py, model.joblib")
    p.add_argument("--dir", default=".", help="parent folder (default: current)")
    p.set_defaults(fn=cmd_new)
    p = sub.add_parser("data", help="data catalog tools")
    dsub = p.add_subparsers(dest="data_cmd", required=True)
    p = dsub.add_parser("export", help="export bars to one parquet file (for developers without QOS)")
    p.add_argument("instrument", help="EUR/USD, GBPUSD, …")
    p.add_argument("tf", help="1m, 5m, 15m, 30m")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--catalog", default=None, help="data catalog folder (default: QOS_CATALOG or the data/catalog of this QOS)")
    p.set_defaults(fn=cmd_data_export)
    p = dsub.add_parser("list", help="datasets in the QOS Cloud data store and on this computer")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_data_cloud)
    p = dsub.add_parser("pull", help="download a dataset from QOS Cloud (checks every file)")
    p.add_argument("dataset")
    p.add_argument("--version", default=None)
    p.set_defaults(fn=cmd_data_cloud)
    p = dsub.add_parser("buy", help="buy access to a dataset (opens the payment page)")
    p.add_argument("dataset")
    p.add_argument("--plan", choices=["one_time", "month", "year"], default=None)
    p.set_defaults(fn=cmd_data_cloud)
    p = dsub.add_parser("check", help="re-check every file of a downloaded dataset")
    p.add_argument("dataset")
    p.set_defaults(fn=cmd_data_cloud)
    p = dsub.add_parser("remove", help="delete a downloaded dataset from this computer")
    p.add_argument("dataset")
    p.set_defaults(fn=cmd_data_cloud)
    p = dsub.add_parser("publish", help="admin: upload part of the data catalog as a new dataset version")
    p.add_argument("dataset")
    p.add_argument("version")
    p.add_argument("--title", default="")
    p.add_argument("--sku", default=None, help="product that unlocks it (empty = free for signed-in users)")
    p.add_argument("--description", default="")
    p.add_argument("--notes", default="")
    p.add_argument("--match", action="append", help="bar folder pattern, e.g. '*1-MINUTE*' (repeatable)")
    p.add_argument("--catalog", default=None)
    p.add_argument("--draft", action="store_true", help="upload without publishing")
    p.set_defaults(fn=cmd_data_cloud)
    p = sub.add_parser("home", help="where QOS keeps its files (QOS_HOME)")
    hsub = p.add_subparsers(dest="home_cmd")
    hp = hsub.add_parser("show", help="print every resolved folder (default)")
    hp.add_argument("--json", action="store_true")
    hp = hsub.add_parser("init", help="create a new QOS_HOME layout (does not overwrite)")
    hp.add_argument("folder")
    p.set_defaults(fn=cmd_home, json=False)
    p = sub.add_parser("backup", help="back up QOS data (history, soak, risk, nodes, configs)")
    p.add_argument("backup_cmd", nargs="?", choices=["run", "list"], default="run")
    p.add_argument("--dest", default=None, help="folder (default: backup settings → $QOS_HOME/backups)")
    p.add_argument("--with-env", action="store_true", help="also include .env (credentials) — keep that backup private")
    p.set_defaults(fn=cmd_backup)
    p = sub.add_parser("restore", help="restore a backup (stop the API first)")
    p.add_argument("file")
    p.add_argument("--with-env", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_restore)
    p = sub.add_parser("publish", help="submit an agent package to QOS Cloud for review (developer account)")
    p.add_argument("folder")
    p.add_argument("--title", default="")
    p.add_argument("--description", default="")
    p.add_argument("--check", action="store_true", help="run the full Cloud checks locally first (backtest + walk-forward)")
    p.add_argument("--catalog", default=None, help="data for --check (default: your data catalog)")
    p.add_argument("--source", action="store_true", help="distribute readable source (default: buyers get a compiled build)")
    p.set_defaults(fn=cmd_publish)
    p = sub.add_parser("agent", help="QOS Cloud agents: status of your submissions; review queue (admin)")
    p.add_argument("agent_cmd", choices=["status", "withdraw", "queue", "approve", "reject", "recheck", "list", "unlist", "restart", "build", "price"])
    p.add_argument("--monthly", type=float, default=None)
    p.add_argument("--yearly", type=float, default=None)
    p.add_argument("--once", type=float, default=None)
    p.add_argument("--remove", choices=["one_time", "month", "year"], default=None)
    p.add_argument("--no-upload", action="store_true", help="build: compile and test only")
    p.add_argument("--out", default=None, help="build: keep the compiled package in this folder")
    p.add_argument("ref", nargs="?")
    p.add_argument("--notes", default="")
    p.add_argument("--status", default="in_review", help="queue filter (in_review, check_failed, submitted, checking)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_agent)
    p = sub.add_parser("feed", help="admin: send live 1-minute bars to QOS Cloud for forward paper")
    p.add_argument("feed_cmd", choices=["status", "push"], nargs="?", default="status")
    p.add_argument("--days", type=int, default=30, help="history to send the first time")
    p.add_argument("--instrument", action="append")
    p.set_defaults(fn=cmd_feed)
    p = sub.add_parser("store", help="QOS Store: agents listed on QOS Cloud — list, show, install")
    p.add_argument("store_cmd", choices=["list", "show", "install", "buy"], nargs="?", default="list")
    p.add_argument("agent", nargs="?")
    p.add_argument("--version", default=None)
    p.add_argument("--plan", choices=["one_time", "month", "year"], default=None, help="buy: which price")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_store)
    p = sub.add_parser("billing", help="QOS Cloud purchases: status, manage payment, upgrade plan")
    p.add_argument("billing_cmd", choices=["status", "portal", "upgrade"], nargs="?", default="status")
    p.add_argument("item", nargs="?", help="upgrade: plan name (pro, team)")
    p.add_argument("--plan", choices=["one_time", "month", "year"], default=None)
    p.set_defaults(fn=cmd_billing)
    p = sub.add_parser("payouts", help="developer: payout account and earnings")
    p.add_argument("payouts_cmd", choices=["status", "onboard"], nargs="?", default="status")
    p.set_defaults(fn=cmd_payouts)
    p = sub.add_parser("login", help="sign this computer in to QOS Cloud (device code)")
    p.add_argument("--name", default=None, help="device name shown in your account (default: computer name)")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--force", action="store_true", help="sign out first if already signed in")
    p.set_defaults(fn=cmd_login)
    p = sub.add_parser("logout", help="sign out of QOS Cloud and remove this device from the account")
    p.set_defaults(fn=cmd_logout)
    p = sub.add_parser("cloud", help="QOS Cloud: status, sync, devices")
    p.add_argument("cloud_cmd", nargs="?", choices=["status", "sync", "devices", "remove"], default="status")
    p.add_argument("device_id", nargs="?")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_cloud)
    p = sub.add_parser("operator", help="Terminal operators (sign-in with PIN): add the first admin, list, reset a PIN")
    osub = p.add_subparsers(dest="op_cmd", required=True)
    op = osub.add_parser("add", help="add an operator (asks for the PIN)")
    op.add_argument("name")
    op.add_argument("--role", default="admin", choices=["admin", "trader", "risk", "viewer", "assistant"])
    op = osub.add_parser("list", help="list operators")
    op = osub.add_parser("pin", help="set a new PIN for an operator")
    op.add_argument("id")
    p.set_defaults(fn=cmd_operator)
    a = ap.parse_args(argv)
    return a.fn(a)


def _ask_pin(minimum: int) -> str:
    import getpass
    pin = getpass.getpass(f"PIN (at least {minimum} characters): ")
    if pin != getpass.getpass("PIN again: "):
        raise SystemExit("PINs do not match")
    return pin


def cmd_operator(a) -> int:
    """Operators live in QOS_HOME/data/operators.json; the running API reads the file on every check, no restart needed"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from api import operators as ops
    try:
        if a.op_cmd == "add":
            o = ops.create(a.name, a.role, _ask_pin(ops.min_pin()), created_by="cli")
            print(f"added {o['id']} ({o['role']}) · sign in on the Terminal with this PIN")
        elif a.op_cmd == "pin":
            ops.update(a.id, pin=_ask_pin(ops.min_pin()))
            print(f"new PIN set for {a.id}")
        else:
            rows = ops.list_ops()
            for o in rows:
                print(f"{o['id']:<20} {o['role']:<10} {'active' if o.get('active', True) else 'disabled'}")
            if not rows:
                print("no operators yet · add one: qos operator add <name> --role admin")
    except (ValueError, KeyError) as e:
        raise SystemExit(f"error: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
