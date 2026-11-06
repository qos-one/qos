"""Step 17: QOS_HOME — existing machines keep every file at its old path · a new QOS_HOME = everything under that folder · no personal values in code"""
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from qos import paths

ROOT = Path(__file__).resolve().parent.parent
OVERRIDES = ("QOS_HOME", "QOS_DATA_DIR", "QOS_CONFIG_DIR", "QOS_REPORTS_DIR", "QOS_MODELS_DIR", "QOS_STRATEGIES_DIR", "IBGW_DIR",
             "QOS_CATALOG", "QOS_ENV_FILE")


@pytest.fixture()
def clean_env(monkeypatch):
    for k in OVERRIDES:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_legacy_layout_is_unchanged_without_qos_home(clean_env):
    assert paths.legacy() and paths.is_source_tree()
    assert paths.data_dir() == ROOT / "data"
    assert paths.config_dir() == ROOT / "configs" / "strategies"
    assert paths.reports_dir() == ROOT / "reports"
    assert paths.models_dir() == ROOT / "models"
    assert paths.env_file() == ROOT / ".env"
    assert paths.strategies_dir() == ROOT.parent / "strategies"
    assert paths.ibgw_dir() == ROOT.parent / "ibgw"
    assert paths.catalog_dir() == ROOT / "data" / "catalog"
    assert paths.state_dir() == ROOT / "data" / "strategy_state"


def test_qos_home_puts_everything_under_it(clean_env, tmp_path):
    clean_env.setenv("QOS_HOME", str(tmp_path / "Q"))
    h = tmp_path / "Q"
    assert not paths.legacy() and paths.home() == h
    assert paths.data_dir() == h / "data" and paths.config_dir() == h / "configs" / "strategies"
    assert paths.reports_dir() == h / "reports" and paths.models_dir() == h / "models"
    assert paths.strategies_dir() == h / "strategies" and paths.ibgw_dir() == h / "ibgw"
    assert paths.env_file() == h / ".env" and paths.catalog_dir() == h / "data" / "catalog"
    assert paths.bot_dir() == ROOT
    clean_env.setenv("QOS_CATALOG", str(tmp_path / "shared_catalog"))
    clean_env.setenv("QOS_DATA_DIR", str(tmp_path / "fast_disk"))
    assert paths.catalog_dir() == tmp_path / "shared_catalog" and paths.data_dir() == tmp_path / "fast_disk"


def test_modules_follow_qos_home(tmp_path):
    """Core modules read paths at import → run in a new process with QOS_HOME set"""
    code = ("import json; from api import server, node_manager, nautilus_bridge, soak, history, gateways, operators, strategy_lab, catalog;"
            "from qx_strategies import reports, registry; from qos import paths;"
            "print(json.dumps({k: str(v) for k, v in {'server': server.DATA_DIR, 'nodes': node_manager.NODES_FILE, 'cfg': node_manager.CONFIG_DIR,"
            "'lab': strategy_lab.CONFIG_DIR, 'logs': nautilus_bridge.LOG_DIR, 'soak': soak.DB_FILE, 'hist': history.DB_FILE, 'gw': gateways.IBGW_ROOT,"
            "'ops': operators.OPS_FILE, 'cat': catalog.CATALOG_DIR, 'rep': reports.REPORTS, 'pkgs': registry.strategies_dir(), 'models': paths.models_dir()}.items()}))")
    env = {k: v for k, v in os.environ.items() if k not in OVERRIDES}
    env["QOS_HOME"] = str(tmp_path / "Q")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-1500:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    h = str(tmp_path / "Q")
    assert all(v.startswith(h) for v in got.values()), {k: v for k, v in got.items() if not v.startswith(h)}


def test_init_home_creates_layout_without_overwriting(tmp_path):
    h = tmp_path / "QOS"
    r = paths.init_home(h, log=lambda *_: None)
    for sub in ("data/logs", "data/strategy_state", "configs/strategies", "reports", "models", "strategies", "ibgw"):
        assert (h / sub).is_dir()
    env = h / ".env"
    assert env.exists() and stat.S_IMODE(env.stat().st_mode) == 0o600
    assert "TELEGRAM_BOT_TOKEN=" in env.read_text() and ".env" in r["created"]
    assert any((h / "configs" / "strategies").glob("*.json"))
    env.write_text("TELEGRAM_BOT_TOKEN=mine\n")
    r2 = paths.init_home(h, log=lambda *_: None)
    assert r2["created"] == [] and env.read_text() == "TELEGRAM_BOT_TOKEN=mine\n"


def test_cli_home_show(clean_env, tmp_path, capsys):
    from qos.cli import main
    clean_env.setenv("QOS_HOME", str(tmp_path))
    assert main(["home", "show", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"] == str(tmp_path / "data")


PERSONAL = re.compile(os.environ.get("QOS_PERSONAL_PATTERN") or r"(?!x)x")    # set QOS_PERSONAL_PATTERN to scan for your own values


def _tracked_text_files():
    roots = [ROOT / d for d in ("api", "qx_strategies", "qos", "scripts", "docs", "launchd/templates", "configs", "qos_sdk")]
    files = [ROOT / f for f in ("README.md", ".env.example", "nautilus_runner.py", "mcp_server.py", "main.py", "qos_cli.py", "pyproject.toml")]
    for r in roots:
        files += [p for p in r.rglob("*") if p.is_file() and "__pycache__" not in p.parts and "dist" not in p.parts
                  and p.suffix not in (".pyc", ".joblib", ".onnx", ".whl")]
    return files


def test_no_personal_values_in_code():
    hits = []
    for f in _tracked_text_files():
        try:
            txt = f.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(txt.splitlines(), 1):
            if PERSONAL.search(line):
                hits.append(f"{f.relative_to(ROOT)}:{i}: {line.strip()[:80]}")
    assert not hits, "\n".join(hits)


def test_env_example_covers_every_env_var_secret():
    example = (ROOT / ".env.example").read_text()
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DATABRICKS_HOST", "DATABRICKS_TOKEN", "MCP_BEARER_TOKEN", "ASSISTANT_PIN", "IB_ACCOUNT"):
        assert re.search(rf"^{key}=\s*$", example, re.M) or re.search(rf"^{key}=", example, re.M), key
    assert not re.search(r"^(TELEGRAM_BOT_TOKEN|DATABRICKS_TOKEN|MCP_BEARER_TOKEN|ASSISTANT_PIN)=\S", example, re.M)


def test_launchd_scripts_have_no_machine_paths():
    for f in ("scripts/launchd_run.sh", "scripts/install_launchd.sh", "launchd/templates/api.plist.in", "launchd/templates/catalog.plist.in"):
        txt = (ROOT / f).read_text()
        assert "/Users/" not in txt.replace("/Users/$(id -un)", ""), f
    assert "@BOT@" in (ROOT / "launchd/templates/api.plist.in").read_text()
