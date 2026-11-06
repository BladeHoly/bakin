"""CHANGELOG.md -> desktop/ui/changelog.json (what the app shows as "Novedades").

    python packaging/changelog.py           # write the JSON
    python packaging/changelog.py --check   # exit 1 if it is out of date
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "CHANGELOG.md"
TARGET = ROOT / "desktop" / "ui" / "changelog.json"
HEADING = re.compile(r"^## v?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?)\s*(?:[—-]\s*(\S+))?\s*$")  # a test build too: 0.13.1-prueba.1


def parse(text: str) -> list[dict]:
    releases: list[dict] = []
    for line in text.splitlines():
        if m := HEADING.match(line.strip()):
            releases.append({"version": m.group(1), "date": m.group(2) or "", "items": []})
        elif releases and line.startswith("- "):
            releases[-1]["items"].append(line[2:].strip())
        elif releases and line.startswith("  ") and releases[-1]["items"]:
            releases[-1]["items"][-1] += " " + line.strip()
    return releases


def render() -> str:
    return json.dumps(parse(SOURCE.read_text(encoding="utf-8")), ensure_ascii=False, indent=1) + "\n"


if __name__ == "__main__":
    out = render()
    if "--check" in sys.argv:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        sys.exit(0 if current == out else "desktop/ui/changelog.json is out of date: run packaging/changelog.py")
    TARGET.write_text(out, encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)}")
