import asyncio
import json
import sys

import pytest

from api import strategy_lab as lab_mod
from qx_strategies import reports

CFG = {"strategy": "ema_cross", "instrument": "EUR/USD.IDEALPRO", "tf_minutes": 5, "fast": 10, "slow": 30, "trade_size": "20000"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "cfg").mkdir()
    monkeypatch.setattr(lab_mod, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(reports, "REPORTS", tmp_path / "reports")
    return tmp_path


def test_save_validate_and_list(env):
    c = lab_mod.save_config("ema_test.json", {**CFG, "fast": "8", "junk": 1}, overwrite=False)
    assert c["fast"] == 8 and "junk" not in c and c["strategy_id"] == "QXEmaCross-EMA-TEST"
    with pytest.raises(ValueError, match="exists"):
        lab_mod.save_config("ema_test.json", CFG, overwrite=False)
    with pytest.raises(ValueError):
        lab_mod.save_config("bad.json", {**CFG, "fast": 40, "slow": 30}, overwrite=True)       # fast >= slow
    with pytest.raises(ValueError, match="instrument"):
        lab_mod.save_config("x_y.json", {**CFG, "instrument": "NZD/USD.IDEALPRO"}, overwrite=True)
    with pytest.raises(ValueError, match="file name"):
        lab_mod.save_config("../evil.json", CFG, overwrite=True)
    reports.save_report("backtest", "QXEmaCross-EMA-TEST", {"config": c, "config_file": "ema_test.json", "pnl_usd": -5.0, "closed_positions": 3})
    items = lab_mod.list_configs({"n1": {"config_file": "ema_test.json"}})
    assert items[0]["latest"]["backtest"]["pnl"] == -5.0 and items[0]["used_by"] == ["n1"]


def test_delete_blocked_when_used(env):
    lab_mod.save_config("ema_a.json", CFG, overwrite=False)
    with pytest.raises(ValueError, match="used by"):
        lab_mod.delete_config("ema_a.json", {"n1": {"config_file": "ema_a.json"}})
    lab_mod.delete_config("ema_a.json", {})
    assert not (env / "cfg" / "ema_a.json").exists()


def test_deploy_checks(env):
    lab_mod.save_config("ema_b.json", CFG, overwrite=False)
    assert any("No backtest" in w for w in lab_mod.deploy_checks("ema_b.json"))
    reports.save_report("backtest", "x", {"config": CFG, "config_file": "ema_b.json", "pnl_usd": 50.0, "closed_positions": 9})
    reports.save_report("parity", "x", {"config": CFG, "config_file": "ema_b.json", "passed": False, "match_rate": 0.5, "new": {}})
    w = lab_mod.deploy_checks("ema_b.json")
    assert not any("No backtest" in x for x in w) and any("parity" in x for x in w)


def test_build_cmd():
    lab = lab_mod.Lab()
    cmd = lab.build_cmd("walkforward", "ema_b.json", {"days": 180, "train": 60, "test": 20})
    assert cmd[-7:] == [str(lab_mod.CONFIG_DIR / "ema_b.json"), "--days", "180", "--train", "60", "--test", "20"]
    assert "scripts/walkforward.py" in cmd and "60" in cmd and "20" in cmd
    assert "--start" in lab.build_cmd("backtest", "ema_b.json", {"start": "2026-01-01", "end": "2026-02-01"})
    with pytest.raises(ValueError):
        lab.build_cmd("nope", "ema_b.json", {})


def test_job_lifecycle(env, monkeypatch):
    lab_mod.save_config("ema_c.json", CFG, overwrite=False)
    script = ("import time\nfor k in range(1,4):\n    print(f'  fold {k}/3 test …', flush=True); time.sleep(0.05)\n"
              "print('out-of-sample  $12.00 · 30 trades', flush=True)\nprint('saved → reports/walkforward/X_1.json', flush=True)")
    lab = lab_mod.Lab()
    monkeypatch.setattr(lab, "build_cmd", lambda kind, file, opts: [sys.executable, "-c", script])

    async def main():
        j = await lab.start("walkforward", "ema_c.json", {})
        with pytest.raises(ValueError, match="another job"):
            await lab.start("backtest", "ema_c.json", {})
        for _ in range(100):
            if lab.jobs[j["id"]]["status"] != "running":
                break
            await asyncio.sleep(0.05)
        return lab.jobs[j["id"]]
    job = asyncio.run(main())
    assert job["status"] == "done" and job["progress"] == 1.0
    assert job["report"] == {"kind": "walkforward", "file": "X_1.json"} and job["summary"].startswith("out-of-sample")


def test_job_cancel_and_failure(env, monkeypatch):
    lab_mod.save_config("ema_d.json", CFG, overwrite=False)
    lab = lab_mod.Lab()
    monkeypatch.setattr(lab, "build_cmd", lambda *a: [sys.executable, "-c", "import time; print('working', flush=True); time.sleep(30)"])

    async def main():
        j = await lab.start("backtest", "ema_d.json", {})
        await asyncio.sleep(0.3)
        lab.cancel(j["id"])
        for _ in range(100):
            if lab.jobs[j["id"]]["finished"]:
                break
            await asyncio.sleep(0.05)
        monkeypatch.setattr(lab, "build_cmd", lambda *a: [sys.executable, "-c", "raise SystemExit('boom')"])
        j2 = await lab.start("backtest", "ema_d.json", {})
        for _ in range(100):
            if lab.jobs[j2["id"]]["finished"]:
                break
            await asyncio.sleep(0.05)
        return lab.jobs[j["id"]], lab.jobs[j2["id"]]
    a, b = asyncio.run(main())
    assert a["status"] == "cancelled" and b["status"] == "failed" and b["exit_code"] == 1


def test_save_as_gets_new_strategy_id(env):
    a = lab_mod.save_config("ema_one.json", {**CFG, "strategy_id": "QXEmaCross-X"}, overwrite=False)
    b = lab_mod.save_config("ema_two.json", {**CFG, "strategy_id": "QXEmaCross-X"}, overwrite=False)
    assert a["strategy_id"] == "QXEmaCross-X" and b["strategy_id"] == "QXEmaCross-EMA-TWO"
    assert lab_mod.save_config("ema_one.json", {**CFG, "strategy_id": "QXEmaCross-X"}, overwrite=True)["strategy_id"] == "QXEmaCross-X"
