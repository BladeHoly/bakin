"""PLAN 0.23.1: one-off conversion of app.css / mini.css colours to the palette of
desktop/ui/tokens.css. White and black with transparency snap to the nearest
step of their scale; the accents map exactly; Wallhaven's own palette swatches
(data, not design) stay as they are.

    python packaging/tokenize_colors.py [--write]
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

UI = Path(__file__).resolve().parent.parent / "desktop" / "ui"
FILES = [UI / "app.css", UI / "mini.css"]

# white over the dark window: fills, lines, then text
WHITE = [(0.04, "--c-fill-1"), (0.06, "--c-fill-2"), (0.1, "--c-fill-3"), (0.15, "--c-line"),
         (0.25, "--c-line-strong"), (0.4, "--c-ink-faint"), (0.6, "--c-ink-dim"), (0.82, "--c-ink-soft"),
         (0.94, "--c-ink-strong")]
BLACK = [(0.2, "--c-shade-1"), (0.36, "--c-shade-2"), (0.58, "--c-shade-3"), (0.8, "--c-shade-4")]
EXACT = {
    "#fff": "--c-white", "rgb(255 255 255)": "--c-white",
    "#0d0c14": "--c-base", "#121118": "--c-base-2", "#12111a": "--c-base-2",
    "rgb(14 13 22 / 0.86)": "--c-base-glass",
    "rgb(90 210 150)": "--c-ok",
    "rgb(255 120 150)": "--c-fav",
    "rgb(255 196 90)": "--c-warn", "rgb(255 205 120)": "--c-warn-soft",
    "rgb(255 110 110)": "--c-danger", "rgb(255 140 140)": "--c-danger-soft", "rgb(255 150 150)": "--c-danger-soft",
    "rgb(220 70 70)": "--c-danger-strong", "#cc3333": "--c-danger-strong",
    "#c42b1c": "--c-close",            # Windows' own close-button red
    "rgb(150 170 255)": "--c-info",
    "#241703": "--c-on-warn",
}
DATA = {"#ffcc33", "#77cc33", "#0099cc", "#663399"}  # Wallhaven colour swatches
COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)")


def snap(alpha: float, scale):
    return min(scale, key=lambda s: abs(s[0] - alpha))[1]


def token(c: str, stats: Counter) -> str:
    norm = re.sub(r"\s+", " ", c.strip()).lower()
    if norm in DATA:
        return c
    if norm in EXACT:
        stats["exact"] += 1
        return f"var({EXACT[norm]})"
    m = re.fullmatch(r"rgba?\((\d+) (\d+) (\d+) / ([\d.]+)\)", norm)
    if m:
        rgb, a = tuple(int(x) for x in m.groups()[:3]), float(m.group(4))
        if rgb == (255, 255, 255):
            stats["white"] += 1
            return f"var({snap(a, WHITE)})"
        if rgb == (0, 0, 0):
            stats["black"] += 1
            return f"var({snap(a, BLACK)})"
        base = EXACT.get(f"rgb({rgb[0]} {rgb[1]} {rgb[2]})")
        if base:  # an accent with transparency
            stats["mix"] += 1
            return f"color-mix(in srgb, var({base}) {round(a * 100)}%, transparent)"
    stats["left"] += 1
    print("  left as is:", c)
    return c


def convert(css: str, stats: Counter) -> str:
    out, pos = [], 0
    for m in re.finditer(r"/\*.*?\*/", css, re.S):
        out.append(COLOR.sub(lambda c: token(c.group(0), stats), css[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(COLOR.sub(lambda c: token(c.group(0), stats), css[pos:]))
    return "".join(out)


def main() -> None:
    for f in FILES:
        stats: Counter = Counter()
        css = f.read_text(encoding="utf-8")
        new = convert(css, stats)
        print(f"{f.name}: {dict(stats)}")
        if "--write" in sys.argv and new != css:
            f.write_text(new, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
