"""Synced lyrics: LRC parsing, LRCLIB lookups and the local .lrc files
(PLAN 0.16.3, opt-in).

Only a song's title, artist, album and duration are ever sent to LRCLIB: no
file names, no paths, no account. A 429 raises Busy (with the server's
Retry-After) so the caller pauses instead of retrying in a loop; nothing
here retries by itself. A song without lyrics is not an error (None).

API docs: https://lrclib.net/docs
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

import requests

from . import __version__
from .paths import app_dir, ensure_private_dir

API = "https://lrclib.net/api"
USER_AGENT = f"Bakin/{__version__} (https://github.com/BladeHoly/bakin)"
TIMEOUT = 15
DUR_SLACK = 2.0           # LRCLIB matches a song's length within ±2 s
DEFAULT_RETRY_AFTER = 60


class LyricsError(Exception):
    pass


class Busy(LyricsError):
    """LRCLIB answered 429: wait `retry_after` seconds before asking again."""

    def __init__(self, message: str, retry_after: int = DEFAULT_RETRY_AFTER):
        super().__init__(message)
        self.retry_after = retry_after


# ---------------------------------------------------------------------- LRC

# [mm:ss], [mm:ss.xx], [mm:ss:xx] (some files use a colon), [m:ss.x]
_STAMP = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_TAG = re.compile(r"^\[([A-Za-z#][A-Za-z0-9_#]*):(.*)\]$")
_OFFSET = re.compile(r"^[+-]?\d+$")


def _seconds(mm: str, ss: str, frac: str | None) -> float:
    t = int(mm) * 60 + int(ss)
    if frac:
        t += int(frac) / (10 ** len(frac))
    return t


def parse_lrc(text: str) -> dict:
    """{"synced": [[seconds, line], …] sorted by time, "plain": str}.
    Text without any time stamp is plain-only (synced == [])."""
    text = str(text or "").lstrip("﻿")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    offset = 0.0
    synced: list[list] = []
    plain: list[str] = []
    order = 0
    keyed: list[tuple[float, int, str]] = []
    for raw in lines:
        line = raw.strip()
        tag = _TAG.match(line)
        if tag and not _STAMP.match(line):
            name, value = tag.group(1).lower(), tag.group(2).strip()
            if name == "offset" and _OFFSET.match(value):
                offset = int(value) / 1000.0
            continue  # metadata ([ar:], [ti:], [length:]…) is not a lyric
        stamps: list[float] = []
        rest = line
        while m := _STAMP.match(rest):
            stamps.append(_seconds(*m.groups()))
            rest = rest[m.end():]
        rest = rest.strip()
        for t in stamps:
            keyed.append((t, order, rest))
            order += 1
        plain.append(rest)
    if not keyed:
        return {"synced": [], "plain": text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")}
    keyed.sort(key=lambda k: (k[0], k[1]))  # stable: same stamp keeps file order
    for t, _, line in keyed:
        synced.append([round(max(0.0, t - offset), 3), line])
    return {"synced": synced, "plain": "\n".join(plain).strip("\n")}


# ------------------------------------------------------------------- LRCLIB

_session: requests.Session | None = None


def _default_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def _close(resp) -> None:
    close = getattr(resp, "close", None)
    if close:
        close()


def _retry_after(resp) -> int:
    try:
        value = int(str((resp.headers or {}).get("Retry-After") or "").strip())
        return value if value > 0 else DEFAULT_RETRY_AFTER
    except ValueError:
        return DEFAULT_RETRY_AFTER


def _get(path: str, params: dict, *, session):
    s = session if session is not None else _default_session()
    try:
        resp = s.get(f"{API}/{path}", params=params,
                     headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=TIMEOUT)
    except requests.Timeout:
        raise LyricsError("LRCLIB tardó demasiado en responder.") from None
    except (requests.RequestException, OSError):
        raise LyricsError("No se pudo conectar con LRCLIB.") from None
    if resp.status_code == 429:
        wait = _retry_after(resp)
        _close(resp)
        raise Busy("LRCLIB pidió esperar", wait)
    return resp


def _json(resp):
    """The response's JSON, None on 404; LyricsError on any other failure."""
    try:
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise LyricsError(f"LRCLIB respondió con un error ({resp.status_code}).")
        try:
            return resp.json()
        except ValueError:
            raise LyricsError("LRCLIB devolvió una respuesta que no se entiende.") from None
    finally:
        _close(resp)


def _duration(duration: float | None) -> float | None:
    try:
        d = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        return None
    return d if d and d > 0 else None


def _result(rec: dict) -> dict:
    return {"lrc": rec.get("syncedLyrics") or None, "plain": rec.get("plainLyrics") or "",
            "instrumental": bool(rec.get("instrumental")), "source": "lrclib"}


def lookup(title: str, artist: str, album: str, duration: float | None, *, session=None) -> dict | None:
    """The song's lyrics from LRCLIB, or None when it has none (not an error):
    {"lrc": str | None, "plain": str, "instrumental": bool, "source": "lrclib"}.
    Tries /api/get (needs album and duration), and on a 404 or without them
    one /api/search whose result must be within 2 s of `duration`.
    Raises Busy on 429 and LyricsError when LRCLIB can't be reached."""
    title, artist, album = (" ".join(str(v or "").split()) for v in (title, artist, album))
    if not title or not artist:
        return None
    dur = _duration(duration)
    if album and dur is not None:
        data = _json(_get("get", {"track_name": title, "artist_name": artist, "album_name": album,
                                  "duration": int(round(dur))}, session=session))
        if isinstance(data, dict):
            return _result(data)
    found = _json(_get("search", {"track_name": title, "artist_name": artist}, session=session))
    for rec in found if isinstance(found, list) else []:
        if not isinstance(rec, dict):
            continue
        if dur is not None:
            d = rec.get("duration")
            if not isinstance(d, (int, float)) or abs(d - dur) > DUR_SLACK:
                continue
        if rec.get("syncedLyrics") or rec.get("plainLyrics") or rec.get("instrumental"):
            return _result(rec)
    return None


# ------------------------------------------------------------- local files

def _key(song_path: Path) -> str:
    norm = os.path.normcase(os.path.abspath(str(song_path)))
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def lyrics_dir() -> Path:
    return app_dir() / "music" / "lyrics"


def local_path(song_path: Path, beside: bool) -> Path:
    """Where a song's .lrc lives: `<stem>.lrc` next to it, or
    app_dir()/music/lyrics/<sha1 of the song's path>.lrc."""
    song_path = Path(song_path)
    if beside:
        return song_path.with_suffix(".lrc")
    return lyrics_dir() / f"{_key(song_path)}.lrc"


def find(song_path: Path) -> str | None:
    """The song's .lrc text, next to it or in the lyrics folder, or None."""
    for beside in (True, False):
        p = local_path(song_path, beside)
        try:
            if p.is_file():
                return p.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
    return None


def save(song_path: Path, lrc_or_plain: str, beside: bool) -> Path:
    """Write the lyrics (synced LRC or plain text, as-is) in UTF-8 without a
    BOM, atomically. Returns the file's path."""
    dest = local_path(song_path, beside)
    if beside:
        dest.parent.mkdir(parents=True, exist_ok=True)
    else:
        ensure_private_dir(dest.parent)
    fd, tmp = tempfile.mkstemp(prefix=".lyrics-", suffix=".tmp", dir=dest.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(str(lrc_or_plain or "").lstrip("﻿"))
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return dest
