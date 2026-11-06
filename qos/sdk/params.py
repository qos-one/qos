"""Param: a strategy parameter declared as a class attribute and read via self.p.<name>"""
from __future__ import annotations

import re
from typing import Any

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
TYPE_OF = {bool: "bool", int: "int", float: "float", str: "str"}       # bool must come before int


def _type_name(v: Any) -> str:
    for t, name in TYPE_OF.items():
        if isinstance(v, t):
            return name
    raise TypeError(f"unsupported param default {v!r} (use int, float, str or bool)")


class Param:
    """Param(default, min=, max=, step=, grid=, help=, label=)

    Type is inferred from default (bool/int/float/str) · grid = values walk-forward tries · read the actual value via self.p.<name>
    """

    def __init__(self, default: Any, *, min: float | None = None, max: float | None = None, step: float | None = None,
                 grid: list | None = None, help: str = "", label: str = ""):
        self.default = default
        self.type = _type_name(default)
        self.min, self.max, self.step = min, max, step
        self.grid = list(grid) if grid else None
        self.help, self.label = help, label
        self.name = ""                              # filled in by __set_name__

    def __set_name__(self, owner, name: str) -> None:
        self.name = name

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        return obj.p.__dict__[self.name]            # self.<name> also works, same as self.p.<name>

    def __set__(self, obj, value) -> None:
        raise AttributeError(f"param '{self.name}' is read-only (set it in the node config)")

    def spec(self) -> dict:
        """Same format as the legacy qx_strategies PARAMS (the Terminal form reads from this)"""
        return {"key": self.name, "field": self.name, "label": self.label or self.name.replace("_", " ").title(),
                "type": self.type, "default": self.default, "min": self.min, "max": self.max, "step": self.step, "help": self.help}

    def check(self) -> list[str]:
        errs = []
        if not NAME_RE.match(self.name or ""):
            errs.append(f"param name '{self.name}' must be snake_case (a-z, 0-9, _)")
        if self.type in ("int", "float"):
            if self.min is not None and self.default < self.min:
                errs.append(f"param '{self.name}': default {self.default} < min {self.min}")
            if self.max is not None and self.default > self.max:
                errs.append(f"param '{self.name}': default {self.default} > max {self.max}")
            for g in self.grid or []:
                if not isinstance(g, (int, float)) or isinstance(g, bool):
                    errs.append(f"param '{self.name}': grid value {g!r} is not a number")
                elif (self.min is not None and g < self.min) or (self.max is not None and g > self.max):
                    errs.append(f"param '{self.name}': grid value {g} outside [{self.min}, {self.max}]")
        elif self.grid and any(_type_name(g) != self.type for g in self.grid):
            errs.append(f"param '{self.name}': grid values must be {self.type}")
        return errs


class ParamValues:
    """Parameter values of an instance (self.p) — read-only"""

    def __init__(self, values: dict):
        self.__dict__.update(values)

    def __setattr__(self, k, v):
        raise AttributeError("params are read-only")

    def __repr__(self) -> str:
        return "p(" + ", ".join(f"{k}={v!r}" for k, v in self.__dict__.items()) + ")"

    def as_dict(self) -> dict:
        return dict(self.__dict__)
