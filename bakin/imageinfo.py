"""Size and dominant colour of the library's images (PLAN 0.13.3), for the
«Fondos» filters: orientation, minimum resolution and colour.

Kept in a private cache (``BakinData/imageinfo.json``) keyed by path, size and
mtime, so a changed file is measured again. The size comes from the file's
header (cheap, read while scanning); the colour needs the pixels, so it is
worked out later, in the background, from the cached thumbnail when there is
one. Wallpapers from Wallhaven come with both from the API.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from .paths import app_dir

# Wallhaven's palette (wallhaven.COLORS): what its colour filter accepts, and
# what the library's chips offer, so both speak the same colours.
PALETTE = ("660000", "990000", "cc0000", "cc3333", "ea4c88", "993399", "663399", "333399", "0066cc", "0099cc",
           "66cccc", "77cc33", "669900", "336600", "666600", "999900", "cccc33", "ffff00", "ffcc33", "ff9900",
           "ff6600", "cc6633", "996633", "663300", "000000", "999999", "cccccc", "ffffff", "424153")
_RGB = [tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)) for h in PALETTE]
SAMPLE = 24  # the colour is counted over a 24×24 copy

_lock = threading.Lock()
_data: dict | None = None
_data_for: Path | None = None  # the data folder it was read from (tests change it)
_dirty = False


def _file() -> Path:
    return app_dir() / "imageinfo.json"


def _load() -> dict:
    global _data, _data_for, _dirty
    if _data is not None and _data_for != _file():
        _data, _dirty = None, False
    if _data is None:
        _data_for = _file()
        try:
            raw = json.loads(_file().read_text(encoding="utf-8"))
            _data = raw.get("items") if isinstance(raw, dict) and isinstance(raw.get("items"), dict) else {}
        except (OSError, ValueError):
            _data = {}
    return _data


def flush() -> None:
    """Writes the cache if something new was measured."""
    global _dirty
    with _lock:
        if not _dirty or _data is None or _data_for != _file():
            return
        p = _file()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix="imageinfo", suffix=".tmp", dir=p.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                json.dump({"schema": 1, "items": _data}, fp, separators=(",", ":"))
            os.replace(tmp, p)
        except OSError:
            return  # measured again next time
        _dirty = False


def key(path: str, size: int, mtime: int) -> str:
    return f"{path}|{size}|{mtime}"


def _put(k: str, **kw) -> None:
    global _dirty
    with _lock:
        rec = _load().setdefault(k, {})
        rec.update(kw)
        _dirty = True


def dims(path: str, size: int, mtime: int) -> tuple[int, int] | None:
    """(width, height), from the cache or the file's header."""
    k = key(path, size, mtime)
    with _lock:
        rec = _load().get(k)
    if rec and rec.get("w"):
        return rec["w"], rec["h"]
    try:
        from PIL import Image
        with Image.open(path) as im:  # lazy: reads the header only
            w, h = im.size
    except Exception:
        return None
    _put(k, w=int(w), h=int(h))
    return w, h


def color(path: str, size: int, mtime: int) -> str | None:
    """The dominant colour already worked out, if any."""
    with _lock:
        rec = _load().get(key(path, size, mtime))
    return (rec or {}).get("c")


def nearest(rgb: tuple[int, int, int]) -> str:
    r, g, b = rgb
    best = min(range(len(_RGB)), key=lambda i: (r - _RGB[i][0]) ** 2 + (g - _RGB[i][1]) ** 2 + (b - _RGB[i][2]) ** 2)
    return PALETTE[best]


def dominant(im) -> str:
    """The palette colour most of the picture is closest to."""
    raw = im.convert("RGB").resize((SAMPLE, SAMPLE)).tobytes()
    counts: dict[str, int] = {}
    for i in range(0, len(raw), 3):
        c = nearest((raw[i], raw[i + 1], raw[i + 2]))
        counts[c] = counts.get(c, 0) + 1
    return max(counts, key=counts.get)


def measure_color(item: dict, thumb: Path | None = None) -> str | None:
    """Works out and keeps an image's dominant colour (from its thumbnail when
    there is one: much quicker than the full file)."""
    from PIL import Image
    try:
        src = thumb if thumb is not None and thumb.is_file() else item["path"]
        with Image.open(src) as im:
            im.draft("RGB", (SAMPLE * 4, SAMPLE * 4))
            c = dominant(im)
    except Exception:
        c = ""  # unreadable: don't try again until the file changes
    _put(key(item["path"], item["size"], item["mtime"]), c=c)
    return c or None


def remember(path: Path, width, height, colors) -> None:
    """What Wallhaven already said about a downloaded wallpaper."""
    try:
        st = path.stat()
    except OSError:
        return
    kw = {}
    if width and height:
        kw.update(w=int(width), h=int(height))
    first = str((colors or [""])[0]).lstrip("#").lower()
    if first in PALETTE:
        kw["c"] = first
    elif first:
        try:
            kw["c"] = nearest(tuple(int(first[i:i + 2], 16) for i in (0, 2, 4)))
        except ValueError:
            pass
    if kw:
        _put(key(str(path), st.st_size, int(st.st_mtime)), **kw)
        flush()


def orientation(w: int, h: int) -> str:
    """landscape / portrait / square (within 5 %)."""
    if abs(w - h) <= max(w, h) * 0.05:
        return "square"
    return "portrait" if h > w else "landscape"
