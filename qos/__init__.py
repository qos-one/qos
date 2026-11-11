"""QOS — Quant Operating System. Package for external developers (qos.sdk)"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _dist_version


def package_version() -> str:
    """Release version: this checkout's pyproject.toml, else installed package metadata.

    The string lives in ``[project].version``. A source tree reads that file. An
    installed copy (no checkout pyproject beside the package) reads the ``qos``
    distribution, then ``qos-sdk`` for a wheel that ships this module under that name.
    """
    try:
        import tomllib
        from pathlib import Path

        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        if pyproject.is_file():
            project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
            if project.get("name") == "qos" and project.get("version"):
                return str(project["version"])
    except Exception:
        pass
    for dist in ("qos", "qos-sdk"):
        try:
            return _dist_version(dist)
        except PackageNotFoundError:
            continue
    return "unknown"


__version__ = package_version()
