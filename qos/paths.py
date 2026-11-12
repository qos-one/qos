"""
All QOS file locations (step 17) — every module asks here instead of hard-coding ~/trading or /Users/<name>

QOS_HOME set (new install / team member machine) → everything lives under QOS_HOME:
    $QOS_HOME/.env  data/  configs/strategies/  reports/  models/  strategies/  ibgw/
QOS_HOME not set and running from QOS source (the bot folder with api/) → original (legacy) layout, nothing to move:
    bot/.env  bot/data/  bot/configs/  bot/reports/  bot/models/  ../strategies/  ../ibgw/
qos-sdk installed with pip (no API code) → QOS_HOME defaults to ~/trading

Override individual folders: QOS_DATA_DIR · QOS_CONFIG_DIR · QOS_REPORTS_DIR · QOS_MODELS_DIR · QOS_STRATEGIES_DIR · IBGW_DIR · QOS_CATALOG · QOS_ENV_FILE
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parent.parent           # bot folder (or site-packages when installed with pip)


def _env_path(name: str) -> Path | None:
    v = os.getenv(name)
    return Path(v).expanduser() if v else None


def is_source_tree() -> bool:
    """Running from the full QOS source (with the API), not qos-sdk from pip"""
    return (CODE_ROOT / "api").is_dir() and (CODE_ROOT / "nautilus_runner.py").exists()


def explicit_home() -> bool:
    return bool(os.getenv("QOS_HOME"))


def legacy() -> bool:
    """Legacy layout: data lives in the code folder (bot/data …) — when QOS_HOME is not set"""
    return not explicit_home() and is_source_tree()


def home() -> Path:
    if explicit_home():
        return Path(os.environ["QOS_HOME"]).expanduser()
    if is_source_tree():
        return CODE_ROOT.parent
    return Path("~/trading").expanduser()


def bot_dir() -> Path:
    """QOS source code (where uv run / nautilus_runner.py run)"""
    return CODE_ROOT


def _under(name: str, legacy_path: Path) -> Path:
    return legacy_path if legacy() else home() / name


def data_dir() -> Path:
    return _env_path("QOS_DATA_DIR") or _under("data", CODE_ROOT / "data")


def config_dir() -> Path:
    """Strategy configs (configs/strategies) used by nodes / Strategy lab"""
    return _env_path("QOS_CONFIG_DIR") or _under("configs", CODE_ROOT / "configs") / "strategies"


def reports_dir() -> Path:
    return _env_path("QOS_REPORTS_DIR") or _under("reports", CODE_ROOT / "reports")


def models_dir() -> Path:
    return _env_path("QOS_MODELS_DIR") or _under("models", CODE_ROOT / "models")


def strategies_dir() -> Path:
    return _env_path("QOS_STRATEGIES_DIR") or home() / "strategies"


def ibgw_dir() -> Path:
    return _env_path("IBGW_DIR") or home() / "ibgw"


def env_file() -> Path:
    return _env_path("QOS_ENV_FILE") or (CODE_ROOT / ".env" if legacy() else home() / ".env")


def catalog_dir() -> Path:
    env = _env_path("QOS_CATALOG")
    if env:
        return env
    if is_source_tree() or explicit_home():
        return data_dir() / "catalog"
    for c in (home() / "data" / "catalog", home() / "bot" / "data" / "catalog"):      # qos-sdk on a machine that has QOS
        if c.exists():
            return c
    return Path.cwd() / "data" / "catalog"


def logs_dir() -> Path:
    return data_dir() / "logs"


def state_dir() -> Path:
    """Strategy self.state across restarts (writable from the node — see qos/sdk/sandbox.py)"""
    return data_dir() / "strategy_state"


def uv() -> str:
    """uv command: QOS_UV · ~/.local/bin/uv · on PATH"""
    for c in (os.getenv("QOS_UV"), os.path.expanduser("~/.local/bin/uv"), shutil.which("uv")):
        if c and os.path.exists(c):
            return c
    return "uv"


def load_env(override: bool = False) -> Path:
    """Load this machine's .env (credentials are not in the code or git)"""
    p = env_file()
    try:
        from dotenv import load_dotenv
        load_dotenv(p, override=override)
    except ImportError:
        pass
    return p


def summary() -> dict:
    return {"qos_home": str(home()), "layout": "legacy (inside the code folder)" if legacy() else "QOS_HOME",
            "code": str(bot_dir()), "env_file": str(env_file()), "data": str(data_dir()), "configs": str(config_dir()),
            "reports": str(reports_dir()), "models": str(models_dir()), "strategies": str(strategies_dir()),
            "catalog": str(catalog_dir()), "ibgw": str(ibgw_dir()), "logs": str(logs_dir())}


def init_home(path: str | Path, log=print) -> dict:
    """Create a new QOS_HOME layout (never overwrites): folders · .env from .env.example · example configs/models from the code"""
    h = Path(path).expanduser().resolve()
    made = []
    for sub in ("data/logs", "data/strategy_state", "configs/strategies", "reports", "models", "strategies", "ibgw"):
        d = h / sub
        if not d.exists():
            d.mkdir(parents=True)
            made.append(sub)
    env = h / ".env"
    example = CODE_ROOT / ".env.example"
    if not env.exists() and example.exists():
        shutil.copy2(example, env)
        os.chmod(env, 0o600)
        made.append(".env")
    src_cfg = CODE_ROOT / "configs" / "strategies"
    if src_cfg.is_dir() and not any((h / "configs" / "strategies").glob("*.json")):
        for f in src_cfg.glob("*.json"):
            shutil.copy2(f, h / "configs" / "strategies" / f.name)
        made.append("configs/strategies/*.json (examples)")
    token_file = h / "data" / "local_token"
    if not token_file.exists():
        from qos.local_token import ensure_local_token
        ensure_local_token(token_file)
        made.append("data/local_token")
    for d in made:
        log(f"   + {d}")
    return {"home": str(h), "created": made}
