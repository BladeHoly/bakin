"""PLAN 0.23.1–0.23.2: the design rules hold in the stylesheets (docs/DESIGN.md)."""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "desktop" / "ui"


def test_no_loose_sizes_in_the_stylesheets():
    r = subprocess.run([sys.executable, str(ROOT / "packaging" / "css_lint.py")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def test_every_token_used_is_defined():
    defined = set(re.findall(r"(--(?:sp|fs|r)-[\w]+)\s*:", (UI / "tokens.css").read_text(encoding="utf-8")))
    for css in ("app.css", "mini.css"):
        used = set(re.findall(r"var\((--(?:sp|fs|r)-[\w]+)\)", (UI / css).read_text(encoding="utf-8")))
        assert used <= defined, f"{css}: {sorted(used - defined)}"


def test_tokens_load_before_the_page_styles():
    for page, css in (("index.html", "app.css"), ("mini.html", "mini.css")):
        html = (UI / page).read_text(encoding="utf-8")
        assert 0 <= html.find('href="tokens.css"') < html.find(f'href="{css}"'), page


def test_the_lint_catches_loose_values(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("css_lint", ROOT / "packaging" / "css_lint.py")
    lint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lint)
    css = tmp_path / "x.css"
    css.write_text(".a { color: #abc; padding: 7px; font-size: 13px; border-radius: 6px; }\n"
                   ".b { color: var(--c-ok); padding: var(--sp-8); margin: -4px; width: 300px; }\n"
                   ".c { background: rgb(0 0 0 / 0.3); border-color: #ffcc33; }\n", encoding="utf-8")
    found = lint.lint(css)
    assert any("#abc" in f for f in found) and any("7px" in f for f in found)
    assert any("13px" in f for f in found) and any("6px" in f for f in found)
    assert any("rgb(0 0 0 / 0.3)" in f for f in found)
    assert not any(":2:" in f for f in found)              # tokens, a negative margin, a layout width
    assert not any("#ffcc33" in f for f in found)          # Wallhaven's swatch: data
