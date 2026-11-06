"""Runs in a subprocess: imports the package's strategy.py and prints its manifest as JSON (keeps third-party code out of the API process)
usage: python -I qos/sdk/_inspect.py <path/to/strategy.py>"""
from __future__ import annotations

import importlib.util
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))      # run with python -I → add the bot root ourselves


def inspect_file(path: str) -> dict:
    from qos.sdk import Strategy, manifest, validate
    spec = importlib.util.spec_from_file_location("qos_pkg_inspect", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    classes = [v for v in vars(mod).values() if isinstance(v, type) and issubclass(v, Strategy) and v is not Strategy and v.__module__ == mod.__name__]
    if len(classes) != 1:
        return {"ok": False, "error": f"strategy.py must define exactly one qos.sdk.Strategy subclass (found {len(classes)})"}
    cls = classes[0]
    errs = validate(cls)
    if errs:
        return {"ok": False, "error": "; ".join(errs), "name": cls.name, "version": cls.version}
    return {"ok": True, "manifest": manifest(cls), "class": cls.__name__}


def check_config(path: str, cfg: dict) -> dict:
    """Build a real Strategy from the config (calls on_configure) to catch conflicting values — in a subprocess"""
    from qos.sdk import compile
    res = inspect_file(path)
    if not res.get("ok"):
        return res
    import importlib.util
    spec = importlib.util.spec_from_file_location("qos_pkg_inspect", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from qos.sdk import Strategy
    cls = next(v for v in vars(mod).values() if isinstance(v, type) and issubclass(v, Strategy) and v is not Strategy and v.__module__ == mod.__name__)
    m = compile(cls)
    m.Strategy(config=m.Config.from_dict(cfg, source=cfg.get("_source", "EXTERNAL"), params=m.PARAMS))
    return {"ok": True}


if __name__ == "__main__":
    try:
        if len(sys.argv) > 3 and sys.argv[1] == "--check":
            out = check_config(sys.argv[2], json.loads(sys.argv[3]))
        else:
            out = inspect_file(sys.argv[1])
    except Exception as e:                       # noqa: BLE001
        out = {"ok": False, "error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()[-1500:]}
    print(json.dumps(out, default=str))
