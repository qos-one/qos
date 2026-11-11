"""/health reports the qos package version (pyproject.toml / installed metadata)."""
import asyncio
import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from api.server import app, health

ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def test_health_version_matches_package():
    declared = _pyproject_version()
    try:
        installed = version("qos")
    except PackageNotFoundError:
        installed = None
    else:
        assert installed == declared
    body = asyncio.run(health())
    assert body["version"] == declared
    assert app.version == declared
    if installed is not None:
        assert body["version"] == installed
