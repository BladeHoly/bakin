"""Song details from MusicBrainz and covers from the Cover Art Archive
(PLAN 0.14.2, opt-in).

Only a song's title, artist and duration are ever sent: no file names, no
paths, no account. MusicBrainz allows one request a second per client, so
every call here (cover art included) goes through one shared limiter; a 503
or 429 raises Busy so the caller pauses (about a minute) instead of retrying
in a loop. Nothing here retries by itself.

API docs: https://musicbrainz.org/doc/MusicBrainz_API and
https://musicbrainz.org/doc/Cover_Art_Archive/API
"""

from __future__ import annotations

import re
import threading
import time
from typing import Callable

import requests

from . import __version__

API = "https://musicbrainz.org/ws/2"
COVERS = "https://coverartarchive.org"
USER_AGENT = f"Bakin/{__version__} ( https://github.com/BladeHoly/bakin )"
MIN_GAP = 1.0             # seconds between requests (MusicBrainz allows 1 a second)
MIN_SCORE = 90            # MusicBrainz's search score (0-100) a match needs
DUR_SLACK_MS = 3000       # a match's length may differ by at most this much
LIMIT = 5
COVER_MAX = 2 * 1024 * 1024
COVER_TYPES = ("image/jpeg", "image/png")
TIMEOUT = 15
COVER_TIMEOUT = 30

_MBID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# Lucene's special characters (https://lucene.apache.org/core/4_3_0/queryparser/
# org/apache/lucene/queryparser/classic/package-summary.html#Escaping_Special_Characters)
_LUCENE = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')


class MBError(Exception):
    pass


class Busy(MBError):
    """MusicBrainz (or the Cover Art Archive) answered 503/429: pause about a minute."""


# ------------------------------------------------------------------ limiter

class Limiter:
    """At most one request every `gap` seconds, across threads. The clock and
    sleep are injectable so tests don't wait."""

    def __init__(self, gap: float = MIN_GAP, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.gap = gap
        self.clock = clock
        self.sleep = sleep
        self._lock = threading.Lock()
        self._last: float | None = None

    def wait(self) -> None:
        with self._lock:
            if self._last is not None:
                wait = self.gap - (self.clock() - self._last)
                if wait > 0:
                    self.sleep(wait)
            self._last = self.clock()


_limiter = Limiter()
_session: requests.Session | None = None


def _default_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def _get(url: str, *, session, params: dict | None = None, timeout: float = TIMEOUT, stream: bool = False):
    _limiter.wait()
    s = session if session is not None else _default_session()
    try:
        resp = s.get(url, params=params, headers={"User-Agent": USER_AGENT, "Accept": "application/json"
                                                   if not stream else "image/jpeg, image/png"},
                     timeout=timeout, allow_redirects=True, stream=stream)
    except requests.Timeout:
        raise MBError("MusicBrainz tardó demasiado en responder.") from None
    except (requests.RequestException, OSError):
        raise MBError("No se pudo conectar con MusicBrainz.") from None
    if resp.status_code in (429, 503):
        _close(resp)
        raise Busy("MusicBrainz pidió esperar")
    return resp


def _close(resp) -> None:
    close = getattr(resp, "close", None)
    if close:
        close()


# -------------------------------------------------------------------- query

def escape(text: str) -> str:
    """A value safe to put inside a quoted Lucene phrase."""
    return _LUCENE.sub(r"\\\1", " ".join(str(text or "").split()))


def build_query(title: str, artist: str, duration: float | None) -> str:
    q = f'recording:"{escape(title)}" AND artist:"{escape(artist)}"'
    ms = _ms(duration)
    if ms is not None:
        q += f" AND dur:[{max(0, ms - DUR_SLACK_MS)} TO {ms + DUR_SLACK_MS}]"
    return q


def _ms(duration: float | None) -> int | None:
    try:
        d = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        return None
    return int(round(d * 1000)) if d and d > 0 else None


def join_credit(credit: list | None) -> str:
    """An artist-credit as MusicBrainz shows it: «A feat. B», «A & B»…"""
    return "".join(f"{c.get('name') or (c.get('artist') or {}).get('name') or ''}{c.get('joinphrase') or ''}"
                   for c in credit or []).strip()


def _year(date: str | None) -> int | None:
    m = re.match(r"^(\d{4})", str(date or ""))
    return int(m.group(1)) if m else None


def _release_rank(rel: dict) -> tuple:
    group = rel.get("release-group") or {}
    primary = (group.get("primary-type") or "").lower()
    secondary = [s.lower() for s in group.get("secondary-types") or []]
    if primary == "album" and not secondary:
        kind = 0
    elif primary == "ep" and not secondary:
        kind = 1
    elif primary == "single" and not secondary:
        kind = 2
    elif "compilation" in secondary:
        kind = 4
    else:
        kind = 3
    date = str(rel.get("date") or "")
    return (0 if (rel.get("status") or "").lower() == "official" else 1, kind,
            date if _year(date) else "9999")


def best_release(releases: list | None) -> dict | None:
    """Official over anything else, then a plain album over EP / single /
    compilation, then the earliest date."""
    rels = [r for r in releases or [] if isinstance(r, dict) and r.get("title")]
    return min(rels, key=_release_rank) if rels else None


def _score(rec: dict) -> int:
    try:
        return int(rec.get("score") or 0)
    except (TypeError, ValueError):
        return 0


def pick(recordings: list | None, duration: float | None) -> dict | None:
    """The best acceptable recording of a search, as Bakin's dict, or None."""
    ms = _ms(duration)
    for rec in sorted(recordings or [], key=_score, reverse=True):
        if _score(rec) < MIN_SCORE or not rec.get("id"):
            continue
        if ms is not None:
            length = rec.get("length")
            if not isinstance(length, (int, float)) or abs(length - ms) > DUR_SLACK_MS:
                continue
        rel = best_release(rec.get("releases"))
        return {"mbid": rec["id"], "title": rec.get("title") or "",
                "artist": join_credit(rec.get("artist-credit")),
                "album": rel.get("title") if rel else None,
                "year": _year(rel.get("date")) if rel and _year(rel.get("date")) else _year(rec.get("first-release-date")),
                "release_mbid": rel.get("id") if rel else None}
    return None


# ---------------------------------------------------------------------- api

def lookup(title: str, artist: str, duration: float | None, *, session=None) -> dict | None:
    """The MusicBrainz recording that matches a song, or None when there is
    no confident match (that is not an error). Raises Busy on 503/429 and
    MBError when MusicBrainz can't be reached."""
    if not str(title or "").strip() or not str(artist or "").strip():
        return None
    resp = _get(f"{API}/recording", session=session,
                params={"query": build_query(title, artist, duration), "fmt": "json", "limit": LIMIT})
    try:
        if resp.status_code != 200:
            raise MBError(f"MusicBrainz respondió con un error ({resp.status_code}).")
        try:
            data = resp.json()
        except ValueError:
            raise MBError("MusicBrainz devolvió una respuesta que no se entiende.") from None
    finally:
        _close(resp)
    return pick(data.get("recordings") if isinstance(data, dict) else None, duration)


def cover(release_mbid: str, *, session=None) -> bytes | None:
    """The release's front cover (500 px), JPEG or PNG and at most 2 MB, or
    None when there is none. Raises Busy on 503/429 and MBError otherwise."""
    mbid = str(release_mbid or "").strip().lower()
    if not _MBID.match(mbid):
        return None
    resp = _get(f"{COVERS}/release/{mbid}/front-500", session=session, timeout=COVER_TIMEOUT, stream=True)
    try:
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise MBError(f"Cover Art Archive respondió con un error ({resp.status_code}).")
        ctype = str((resp.headers or {}).get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype not in COVER_TYPES:
            return None
        try:
            if int((resp.headers or {}).get("Content-Length") or 0) > COVER_MAX:
                return None
        except ValueError:
            pass
        buf = bytearray()
        try:
            for chunk in resp.iter_content(1 << 16):
                buf += chunk
                if len(buf) > COVER_MAX:
                    return None
        except (requests.RequestException, OSError):
            raise MBError("Se cortó la descarga de la tapa.") from None
        data = bytes(buf)
        if not (data.startswith(b"\xff\xd8\xff") or data.startswith(b"\x89PNG\r\n\x1a\n")):
            return None
        return data
    finally:
        _close(resp)
