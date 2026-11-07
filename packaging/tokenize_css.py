"""PLAN 0.23.1: one-off conversion of app.css / mini.css to the design tokens of
desktop/ui/tokens.css. Spacing, radii and font sizes snap to the nearest step of
the scale (docs/DESIGN.md); layout dimensions, positions, 1px hairlines,
negative values and anything inside calc() are left alone.

    python packaging/tokenize_css.py [--write]   # without --write: only the report
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

UI = Path(__file__).resolve().parent.parent / "desktop" / "ui"
FILES = [UI / "app.css", UI / "mini.css"]

SPACE = [2, 4, 6, 8, 10, 12, 16, 20, 24, 32]          # --sp-N (px)
FONT = [10, 11, 12, 13, 14, 16, 18, 22, 32]           # --fs-N (px)
RADIUS = [4, 8, 12, 16, 24]                           # --r-N (px); 999px → --r-pill

SPACE_PROPS = re.compile(r"^(margin|padding)(-(top|right|bottom|left|inline|block)(-(start|end))?)?$|^(row-|column-)?gap$")
VALUE = re.compile(r"(?<![\w.-])(\d*\.?\d+)(rem|px)\b")


def nearest(px: float, scale: list[int], round_up: bool = False) -> int:
    best = min(scale, key=lambda s: (abs(s - px), -s if round_up else s))
    return best


def convert_value(prop: str, value: str, stats: Counter) -> str:
    if "calc(" in value or "var(" in value and not VALUE.search(value):
        return value

    def sub(m: re.Match) -> str:
        num, unit = float(m.group(1)), m.group(2)
        px = num * 16 if unit == "rem" else num
        if prop == "border-radius":
            if px >= 999:
                stats["radius"] += 1
                return "var(--r-pill)"
            if px <= 1:
                return m.group(0)
            stats["radius"] += 1
            return f"var(--r-{nearest(px, RADIUS, round_up=True)})"
        if prop == "font-size":
            stats["font"] += 1
            return f"var(--fs-{nearest(px, FONT)})"
        # spacing: hairlines and big layout offsets stay literal
        if px <= 1 or px > 32:
            return m.group(0)
        stats["space"] += 1
        return f"var(--sp-{nearest(px, SPACE)})"

    return VALUE.sub(sub, value)


DECL = re.compile(r"(?<![-\w])([a-z-]+)(\s*:\s*)([^;{}]+)(;|(?=\}))")


def convert(css: str, stats: Counter) -> str:
    out, pos = [], 0
    # leave comments untouched
    for m in re.finditer(r"/\*.*?\*/", css, re.S):
        out.append(convert_code(css[pos:m.start()], stats))
        out.append(m.group(0))
        pos = m.end()
    out.append(convert_code(css[pos:], stats))
    return "".join(out)


def convert_code(code: str, stats: Counter) -> str:
    def decl(m: re.Match) -> str:
        prop, sep, value, end = m.groups()
        if prop.startswith("--"):
            return m.group(0)
        if SPACE_PROPS.match(prop) or prop in ("border-radius", "font-size"):
            if "calc(" in value or value.lstrip().startswith("-"):
                return m.group(0)
            # a negative part inside a shorthand ("0 -4px") stays as is
            if re.search(r"(^|\s)-\d", value):
                return m.group(0)
            return prop + sep + convert_value(prop, value, stats) + end
        return m.group(0)

    return DECL.sub(decl, code)


def main() -> None:
    write = "--write" in sys.argv
    for f in FILES:
        stats: Counter = Counter()
        css = f.read_text(encoding="utf-8")
        new = convert(css, stats)
        print(f"{f.name}: {dict(stats)}")
        if write and new != css:
            f.write_text(new, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
