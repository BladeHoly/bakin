"""PLAN 0.23.2: spacing, radii and type sizes in app.css / mini.css come from
desktop/ui/tokens.css (docs/DESIGN.md). Prints file:line for each loose value
and exits 1 if there is any.

Allowed as literals: 0, auto, 1px hairlines, %, em (relative to the text),
layout offsets over 32 px (a column's width, a sheet's inset) and negative
margins (geometry: centring a slider's thumb, pulling an icon to an edge).
Colours only as palette tokens (`var(--c-*)`, glass-midas' `--gm-*`), except
Wallhaven's colour swatches, which are data."""
from __future__ import annotations

import re
import sys
from pathlib import Path

UI = Path(__file__).resolve().parent.parent / "desktop" / "ui"
FILES = [UI / "app.css", UI / "mini.css"]
SPACE = re.compile(r"^(margin|padding)(-[a-z-]+)?$|^(row-|column-)?gap$")
COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)")
DATA_COLORS = {"#ffcc33", "#77cc33", "#0099cc", "#663399"}  # Wallhaven's swatches
NUM = re.compile(r"(?<![\w.-])-?(\d*\.?\d+)(rem|px)\b")


def loose(prop: str, value: str) -> list[str]:
    value = re.sub(r"var\([^)]*\)", "", value)
    bad = []
    for m in NUM.finditer(value):
        if m.group(0).startswith("-") and prop not in ("font-size", "border-radius"):
            continue
        px = float(m.group(1)) * (16 if m.group(2) == "rem" else 1)
        if prop in ("font-size", "border-radius"):
            bad.append(m.group(0))
        elif px > 1 and px <= 32:
            bad.append(m.group(0))
    return bad


def lint(path: Path) -> list[str]:
    out = []
    text = re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), path.read_text(encoding="utf-8"), flags=re.S)
    for m in re.finditer(r"(?<![-\w])([a-z-]+)\s*:\s*([^;{}]+)(?=;|\})", text):
        prop, value = m.group(1), m.group(2)
        line = text.count("\n", 0, m.start()) + 1
        for c in COLOR.findall(re.sub(r"var\([^)]*\)", "", value)):
            if c.lower() not in DATA_COLORS:
                out.append(f"{path.name}:{line}: {prop}: {c} (use a colour token from tokens.css)")
        if SPACE.match(prop) or prop in ("font-size", "border-radius"):
            for v in loose(prop, value):
                out.append(f"{path.name}:{line}: {prop}: {v} (use a token from tokens.css)")
    return out


def main() -> int:
    problems = [p for f in FILES for p in lint(f)]
    for p in problems:
        print(p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
