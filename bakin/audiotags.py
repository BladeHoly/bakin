"""Audio tags for the music library: title, artist, album, duration, codec,
bitrate, and the YouTube id the file came from (if any).

Read with mutagen (files are only opened for reading) and cached in Bakin's
private data folder, keyed by path + size + modification time, so a large
collection is read once.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path

from .paths import app_dir

LOSSLESS = {"flac", "alac", "pcm", "wav", "aiff"}
YOUTUBE_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|embed/)([A-Za-z0-9_-]{11})")
ID_IN_NAME = re.compile(r"\[([A-Za-z0-9_-]{11})\]")
CACHE_VERSION = 1

_lock = threading.Lock()
_cache: dict | None = None
_dirty = False


def _cache_path() -> Path:
    return app_dir() / "library" / "audio-tags.json"


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            data = json.loads(_cache_path().read_text(encoding="utf-8"))
            _cache = data.get("items", {}) if data.get("v") == CACHE_VERSION else {}
        except (OSError, ValueError):
            _cache = {}
    return _cache


def flush() -> None:
    """Write the cache if it changed (called after a library scan)."""
    global _dirty
    with _lock:
        if not _dirty or _cache is None:
            return
        p = _cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="audio-tags", suffix=".tmp", dir=p.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump({"v": CACHE_VERSION, "items": _cache}, fp, ensure_ascii=False)
        os.replace(tmp, p)
        _dirty = False


def reset_memory_cache() -> None:  # tests
    global _cache, _dirty
    with _lock:
        _cache, _dirty = None, False


def forget(path) -> None:
    """Drops what's cached for a file Bakin just wrote (its size and second may not change)."""
    global _dirty
    prefix = f"{path}|"
    with _lock:
        cache = _load()
        for k in [k for k in cache if k.startswith(prefix)]:
            del cache[k]
            _dirty = True


def info(path: str, size: int, mtime: int) -> dict:
    global _dirty
    key = f"{path}|{size}|{mtime}"
    with _lock:
        cache = _load()
        hit = cache.get(key)
    if hit is not None:
        return hit
    meta = read(Path(path))
    with _lock:
        cache[key] = meta
        _dirty = True
    return meta


def _first(tags, *names) -> str:
    if not tags:
        return ""
    for n in names:
        try:
            v = tags.get(n)
        except (ValueError, KeyError, TypeError):
            v = None
        if v:
            if isinstance(v, list):
                v = v[0]
            v = getattr(v, "text", v)
            if isinstance(v, list):
                v = v[0] if v else ""
            s = str(v).strip()
            if s:
                return s
    return ""


def _codec(f) -> str:
    name = type(f).__name__.lower()
    if name in ("mp3", "easymp3"):
        return "mp3"
    if name == "oggopus":
        return "opus"
    if name == "oggvorbis":
        return "vorbis"
    if name == "flac":
        return "flac"
    if name in ("wave", "aiff"):
        return "pcm"
    if name in ("mp4", "easymp4"):
        c = str(getattr(f.info, "codec", "") or "").lower()
        return "alac" if c.startswith("alac") else "aac"
    return name


def read(path: Path) -> dict:
    out = {"title": "", "artist": "", "album": "", "duration": None, "bitrate": None,
           "codec": path.suffix[1:].lower(), "lossless": False, "yt_id": None}
    m = ID_IN_NAME.search(path.stem)
    if m:
        out["yt_id"] = m.group(1)
    try:
        import mutagen
        f = mutagen.File(path, easy=False)
    except Exception:
        f = None
    if f is None:
        return out
    inf = getattr(f, "info", None)
    if inf is not None:
        if getattr(inf, "length", None):
            out["duration"] = round(float(inf.length), 1)
        br = getattr(inf, "bitrate", None)
        if br:
            out["bitrate"] = int(round(br / 1000))
    out["codec"] = _codec(f)
    out["lossless"] = out["codec"] in LOSSLESS
    if out["lossless"] and not out["bitrate"] and out["duration"]:
        try:
            out["bitrate"] = int(path.stat().st_size * 8 / out["duration"] / 1000)
        except (OSError, ZeroDivisionError):
            pass
    tags = f.tags
    out["title"] = _first(tags, "TIT2", "title", "\xa9nam", "TITLE")
    out["artist"] = _first(tags, "TPE1", "artist", "\xa9ART", "ARTIST", "TPE2", "albumartist", "aART")
    out["album"] = _first(tags, "TALB", "album", "\xa9alb", "ALBUM")
    if not out["yt_id"] and tags:
        try:
            values = [str(v) for v in (tags.values() if hasattr(tags, "values") else [])]
        except Exception:
            values = []
        for v in values:
            m = YOUTUBE_ID.search(v)
            if m:
                out["yt_id"] = m.group(1)
                break
    return out


def cover(path: Path) -> bytes | None:
    """The cover art embedded in an audio file (what yt-dlp's
    --embed-thumbnail writes), as image bytes; None if there is none."""
    try:
        import mutagen
        f = mutagen.File(path, easy=False)
    except Exception:
        return None
    if f is None:
        return None
    pics = getattr(f, "pictures", None)  # FLAC
    if pics:
        return pics[0].data
    tags = f.tags
    if not tags:
        return None
    try:
        if hasattr(tags, "getall"):  # ID3 (MP3)
            apic = tags.getall("APIC")
            if apic:
                front = [a for a in apic if getattr(a, "type", 3) == 3]
                return (front or apic)[0].data
        covr = tags.get("covr") if hasattr(tags, "get") else None  # MP4 / M4A
        if covr:
            return bytes(covr[0])
        block = tags.get("metadata_block_picture") if hasattr(tags, "get") else None  # Opus / Vorbis
        if block:
            import base64
            from mutagen.flac import Picture
            return Picture(base64.b64decode(block[0])).data
    except Exception:
        return None
    return None


_TAG_FIELDS = ("title", "artist", "album", "year")
_ID3_FRAMES = {"title": "TIT2", "artist": "TPE1", "album": "TALB", "year": "TDRC"}
_MP4_KEYS = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb", "year": "\xa9day"}
_VORBIS_KEYS = {"title": "title", "artist": "artist", "album": "album", "year": "date"}


def _tag_file(path: Path):
    """The mutagen file and its tag family ("vorbis", "id3" or "mp4")."""
    import mutagen
    try:
        f = mutagen.File(path)
    except Exception as exc:
        raise ValueError(f"No se pudo leer el archivo: {exc}") from exc
    kind = {"flac": "vorbis", "oggopus": "vorbis", "oggvorbis": "vorbis",
            "mp3": "id3", "mp4": "mp4"}.get(type(f).__name__.lower()) if f is not None else None
    if kind is None:
        raise ValueError("Este formato no admite tags")
    return f, kind


def _field(f, kind: str, field: str) -> str:
    tags = f.tags
    if not tags:
        return ""
    if kind == "id3":
        frame = tags.get(_ID3_FRAMES[field])
        return str(frame.text[0]).strip() if frame and frame.text else ""
    v = tags.get(_MP4_KEYS[field] if kind == "mp4" else _VORBIS_KEYS[field])
    return str(v[0]).strip() if v else ""


def snapshot(path: Path) -> dict:
    """The exact title / artist / album fields of a file (what `write`
    changes), for backups and undo."""
    f, kind = _tag_file(Path(path))
    return {k: _field(f, kind, k) for k in _TAG_FIELDS}


def write(path: Path, *, title: str | None = None, artist: str | None = None,
          album: str | None = None, year: str | None = None) -> dict:
    """Write title / artist / album / year (None = leave as is, "" = remove) and
    return the previous values, for undo. The cover art is not touched."""
    f, kind = _tag_file(Path(path))
    before = {k: _field(f, kind, k) for k in _TAG_FIELDS}
    new = {k: v for k, v in (("title", title), ("artist", artist), ("album", album), ("year", year))
           if v is not None}
    if not new:
        return before
    if f.tags is None:
        f.add_tags()
    tags = f.tags
    for field, value in new.items():
        value = str(value).strip()
        if kind == "id3":
            from mutagen import id3
            frame = _ID3_FRAMES[field]
            tags.delall(frame)
            if value:
                tags.add(getattr(id3, frame)(encoding=3, text=[value]))
        else:
            key = _MP4_KEYS[field] if kind == "mp4" else _VORBIS_KEYS[field]
            if value:
                tags[key] = [value]
            elif key in tags:
                del tags[key]
    if kind == "id3":
        f.save(v2_version=4)
    else:
        f.save()
    return before


def is_low_quality(meta: dict) -> bool:
    """Worth downloading again: MP3 (always a re-encode of what YouTube
    serves), other lossy formats under 160 kbps. YouTube's own Opus (about
    130 kbps) is the source itself, so it only counts below 96 kbps."""
    if meta.get("lossless"):
        return False
    br = meta.get("bitrate") or 0
    if meta.get("codec") == "opus":
        return 0 < br < 96
    return meta.get("codec") == "mp3" or (0 < br < 160)


def write_cover(path: Path, image: bytes | None) -> bytes | None:
    """Embeds `image` (JPEG or PNG) as the front cover, replacing the one there (None removes it),
    and returns the previous cover bytes, for undo (PLAN 0.14.2)."""
    import base64
    from mutagen.flac import Picture
    path = Path(path)
    before = cover(path)
    f, kind = _tag_file(path)
    mime = "image/png" if image and image[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"

    def picture():
        pic = Picture()
        pic.type, pic.mime, pic.data = 3, mime, image
        return pic
    if type(f).__name__.lower() == "flac":
        f.clear_pictures()
        if image:
            f.add_picture(picture())
        f.save()
        return before
    if f.tags is None:
        f.add_tags()
    if kind == "id3":
        from mutagen import id3
        f.tags.delall("APIC")
        if image:
            f.tags.add(id3.APIC(encoding=3, mime=mime, type=3, desc="Cover", data=image))
        f.save(v2_version=4)
    elif kind == "mp4":
        from mutagen.mp4 import MP4Cover
        if image:
            fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
            f.tags["covr"] = [MP4Cover(image, imageformat=fmt)]
        elif "covr" in f.tags:
            del f.tags["covr"]
        f.save()
    else:  # Opus / Vorbis
        if image:
            f.tags["metadata_block_picture"] = [base64.b64encode(picture().write()).decode("ascii")]
        elif "metadata_block_picture" in f.tags:
            del f.tags["metadata_block_picture"]
        f.save()
    return before
