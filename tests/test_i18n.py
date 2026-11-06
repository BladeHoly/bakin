"""The interface's English (PLAN 0.22.3): Spanish is the source and
desktop/ui/i18n/en.js maps each Spanish text to English. Every text the
code translates — tr()/tn() literals in app.js and mini.js, the static texts
of index.html and mini.html (the same ones translateDom() looks up) — must be
in it, with the same {placeholders}; and it must not keep texts nothing uses
any more (a key counts as used when the page code, the page or a string
literal of the engine or the shell has it: their messages go through tr()).
"""
import json
import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "desktop" / "ui"
JS = [UI / "app.js", UI / "mini.js"]
HTML = [UI / "index.html", UI / "mini.html"]
ATTRS = ("title", "aria-label", "placeholder", "heading", "label")  # as in i18n.js


def load_en() -> dict:
    text = (UI / "i18n" / "en.js").read_text(encoding="utf-8")
    return json.loads(text[text.index("{"):text.rindex("}") + 1])


_ESC = {"n": "\n", "t": "\t", "\\": "\\", "'": "'", '"': '"', "`": "`"}


def _unescape(s: str) -> str:
    def one(m):
        e = m.group(1)
        if e.startswith("u{"):
            return chr(int(e[2:-1], 16))
        if e.startswith("u"):
            return chr(int(e[1:], 16))
        return _ESC.get(e, e)
    return re.sub(r"\\(u\{[0-9a-fA-F]+\}|u[0-9a-fA-F]{4}|.)", one, s)


_STR = r"""'((?:[^'\\\n]|\\.)*)'|"((?:[^"\\\n]|\\.)*)\""""


def js_keys(path: Path) -> list:
    """The literal texts handed to tr(text, …) and tn(one, many, …)."""
    src = path.read_text(encoding="utf-8")
    keys = []
    for m in re.finditer(r"\btr\(\s*(?:" + _STR + r")", src):
        keys.append(_unescape(m.group(1) if m.group(1) is not None else m.group(2)))
    for m in re.finditer(r"\btn\(\s*(?:" + _STR + r")\s*,\s*(?:" + _STR + r")", src):
        g = m.groups()
        keys.append(_unescape(g[0] if g[0] is not None else g[1]))
        keys.append(_unescape(g[2] if g[2] is not None else g[3]))
    return keys


def js_template_calls(path: Path) -> list:
    """tr(`…${x}…`) can't be looked up: the text must be a fixed string."""
    src = path.read_text(encoding="utf-8")
    return [m.group(0) for m in re.finditer(r"\bt[rn]\(\s*`[^`]*\$\{", src)]


class _Texts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        for k, v in attrs:
            if k in ATTRS and v:
                self.out.append(" ".join(v.split()))

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.out.append(" ".join(data.split()))


def html_texts(path: Path) -> list:
    """Texts worth translating: with a word in them (not «4K», «31 Hz», «+0 dB»)."""
    p = _Texts()
    p.feed(path.read_text(encoding="utf-8"))
    return [t for t in p.out if re.search(r"[^\W\d_]{3,}", t)]


def _source_literals() -> set:
    """String literals of the engine and the shell, whose messages the page passes through tr()."""
    out = set()
    files = list((ROOT / "bakin").rglob("*.py")) + list((ROOT / "desktop" / "src-tauri" / "src").glob("*.rs"))
    for f in files:
        for m in re.finditer(_STR, f.read_text(encoding="utf-8")):
            out.add(_unescape(m.group(1) if m.group(1) is not None else m.group(2)))
    return out


def test_en_is_valid_and_complete():
    en = load_en()
    assert all(isinstance(v, str) and v.strip() for v in en.values())
    missing = sorted({k for f in JS for k in js_keys(f)} - set(en))
    assert not missing, f"tr() texts without English: {missing[:20]}"
    missing = sorted({t for f in HTML for t in html_texts(f)} - set(en))
    assert not missing, f"page texts without English: {missing[:20]}"


def test_no_template_keys():
    bad = [c for f in JS for c in js_template_calls(f)]
    assert not bad, bad


def test_placeholders_match():
    bad = [k for k, v in load_en().items()
           if set(re.findall(r"\{(\w+)\}", k)) != set(re.findall(r"\{(\w+)\}", v))]
    assert not bad, bad


def test_spaces_at_the_ends_match():
    # «… · {n} ya estaban» is glued to another text: the English keeps the same edges
    bad = [k for k, v in load_en().items() if (k[:1] == " ") != (v[:1] == " ") or (k[-1:] == " ") != (v[-1:] == " ")]
    assert not bad, bad


def test_no_unused_keys():
    used = {k for f in JS for k in js_keys(f)} | {t for f in HTML for t in html_texts(f)} | _source_literals()
    unused = sorted(set(load_en()) - used)
    assert not unused, f"en.js keys nothing uses: {unused[:20]}"


def test_lang_setting():
    from bakin import service
    assert service.DEFAULT_SETTINGS["lang"] == "es"  # Spanish unless chosen (an English Windows too)
    check = service.Service.SETTABLE["lang"]
    assert [check(v) for v in ("es", "en", "auto", "fr", None)] == ["es", "en", "auto", "es", "es"]
