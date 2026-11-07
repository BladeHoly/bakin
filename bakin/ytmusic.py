"""YouTube Music's catalogue, browsed like Wallhaven (PLAN 0.15.1): search, an artist's page, an
album's or a playlist's songs. Without an account (ytmusicapi), nothing is downloaded here: the
songs picked go to the queue as links, like any other.

Polite by default: a search only when asked (Enter), results kept 10 minutes and pages a day, the
covers on disk, at most `HOURLY` catalogue requests an hour (Configuración can lift it), and a
stop of an hour when YouTube says «too many requests» or asks if we're a bot.
"""

from __future__ import annotations

import base64
import hashlib
import io
import threading
import time
from collections import deque
from pathlib import Path

from .paths import app_dir

KINDS = ("songs", "albums", "artists", "playlists")
SEARCH_TTL = 600
PAGE_TTL = 86400
HOURLY = 100
COOLDOWN = 3600
THUMB_HOSTS = (".googleusercontent.com", ".ytimg.com", ".ggpht.com")


class CatalogError(Exception):
    """Something to show the user (Spanish)."""


class Busy(CatalogError):
    """YouTube asked us to slow down, or our own hourly limit: wait until `until`."""

    def __init__(self, message: str, until: float):
        super().__init__(message)
        self.until = until


_lock = threading.Lock()
_client = None
_cache: dict[tuple, tuple[float, object]] = {}
_calls: deque[float] = deque()
_paused_until = 0.0


class _Recorded:
    """The UI test's catalogue: answers from a saved JSON, never the network (only in a test
    copy: BAKIN_HOME must be set too)."""

    def __init__(self, path: str):
        import json
        self.data = json.loads(Path(path).read_text(encoding="utf-8"))

    def search(self, q, filter, limit, ignore_spelling):
        return self.data.get("search", {}).get(filter, [])

    def get_artist(self, browse_id):
        return self.data["artist"]

    def get_album(self, browse_id):
        return self.data["album"]

    def get_playlist(self, playlist_id, limit):
        return self.data.get("playlist", {"title": "", "tracks": []})


def _yt():
    global _client
    import os
    with _lock:
        if _client is None:
            fake = os.environ.get("BAKIN_FAKE_YTMUSIC")
            if fake and os.environ.get("BAKIN_HOME"):
                _client = _Recorded(fake)
            else:
                from ytmusicapi import YTMusic
                _client = YTMusic()  # in English: with language="es" its parsers find no results (1.12.3)
        return _client


def _spend(hourly: int, now: float) -> None:
    """One catalogue request against the hour's budget (0 = no limit)."""
    with _lock:
        if _paused_until > now:
            raise Busy("YouTube pidió esperar: el catálogo vuelve en un rato.", _paused_until)
        while _calls and now - _calls[0] > 3600:
            _calls.popleft()
        if hourly and len(_calls) >= hourly:
            until = _calls[0] + 3600
            raise Busy(f"Llegaste al tope de {hourly} búsquedas por hora (Configuración › Rendimiento).", until)
        _calls.append(now)


def _offline(exc: BaseException) -> bool:
    """No connection (or the site doesn't answer): not a change on YouTube's side (PLAN 0.23.4)."""
    import requests
    seen = exc
    while seen is not None:
        if isinstance(seen, (requests.ConnectionError, requests.Timeout, TimeoutError, ConnectionError)):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


def _blocked(exc: Exception) -> bool:
    text = f"{exc.__class__.__name__} {exc}".lower()
    return any(w in text for w in ("429", "too many requests", "not a bot", "captcha", "sign in to confirm"))


def _ask(key: tuple, ttl: int, hourly: int, fn):
    """A cached catalogue request; YouTube's pushback pauses every request for an hour."""
    global _paused_until
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    _spend(hourly, now)
    try:
        value = fn()
    except Exception as exc:  # ytmusicapi raises plain Exceptions with the HTTP status inside
        if _blocked(exc):
            with _lock:
                _paused_until = now + COOLDOWN
            raise Busy("YouTube pidió esperar: el catálogo vuelve en una hora.", _paused_until) from exc
        if _offline(exc):
            raise CatalogError("No se pudo conectar con YouTube Music: revisa la conexión a internet.") from exc
        raise CatalogError("YouTube Music no respondió como se esperaba. Si sigue, actualiza Bakin.") from exc
    with _lock:
        _cache[key] = (now, value)
        if len(_cache) > 200:  # the oldest go
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:50]:
                del _cache[k]
    return value


def reset() -> None:  # tests
    global _client, _paused_until
    with _lock:
        _client = None
        _cache.clear()
        _calls.clear()
        _paused_until = 0.0


# ------------------------------------------------------------------ cards

def _artists(raw) -> str:
    if isinstance(raw, list):
        return ", ".join(a.get("name", "") for a in raw if isinstance(a, dict) and a.get("name"))
    return str(raw or "")


def _thumb(raw) -> str:
    """The largest thumbnail's URL, asked at 300 px."""
    if not raw:
        return ""
    url = raw[-1].get("url", "") if isinstance(raw, list) else ""
    if "googleusercontent" in url and "=w" in url:
        url = url.split("=w")[0] + "=w300-h300-l90-rj"
    return url


def card(r: dict, kind: str | None = None) -> dict | None:
    """One result, in Bakin's words."""
    kind = kind or {"song": "song", "video": "song", "album": "album", "single": "album", "ep": "album",
                    "artist": "artist", "playlist": "playlist"}.get(str(r.get("resultType") or "").lower())
    if kind == "song" and r.get("videoId"):
        album = r.get("album")
        return {"kind": "song", "id": r["videoId"], "title": r.get("title") or "", "artist": _artists(r.get("artists")),
                "album": album.get("name") if isinstance(album, dict) else (album or ""),
                "duration": r.get("duration_seconds"), "explicit": bool(r.get("isExplicit")),
                "available": r.get("isAvailable", True) is not False,
                "official": r.get("videoType") == "MUSIC_VIDEO_TYPE_ATV", "thumb": _thumb(r.get("thumbnails"))}
    if kind == "album" and r.get("browseId"):
        return {"kind": "album", "id": r["browseId"], "title": r.get("title") or "", "artist": _artists(r.get("artists")),
                "type": r.get("type") or "Álbum", "year": r.get("year") or "", "explicit": bool(r.get("isExplicit")),
                "playlist": r.get("playlistId") or r.get("audioPlaylistId") or "", "thumb": _thumb(r.get("thumbnails"))}
    if kind == "artist" and r.get("browseId"):
        return {"kind": "artist", "id": r["browseId"], "title": r.get("artist") or r.get("title") or "",
                "thumb": _thumb(r.get("thumbnails"))}
    if kind == "playlist" and r.get("browseId"):
        pid = r["browseId"][2:] if r["browseId"].startswith("VL") else r["browseId"]
        return {"kind": "playlist", "id": pid, "title": r.get("title") or "", "artist": r.get("author") or "",
                "count": r.get("itemCount") or "", "thumb": _thumb(r.get("thumbnails"))}
    return None


def search(q: str, kind: str = "songs", hourly: int = HOURLY) -> list[dict]:
    q = " ".join(str(q or "").split())[:200]
    if not q:
        return []
    kind = kind if kind in KINDS else "songs"
    raw = _ask(("search", q.casefold(), kind), SEARCH_TTL, hourly,
               lambda: _yt().search(q, filter=kind, limit=40, ignore_spelling=False))
    one = kind[:-1]
    return [c for c in (card(r, one) for r in raw or []) if c]


def artist(browse_id: str, hourly: int = HOURLY) -> dict:
    a = _ask(("artist", browse_id), PAGE_TTL, hourly, lambda: _yt().get_artist(browse_id))
    def part(name, kind):
        sec = a.get(name) or {}
        return [c for c in (card(r, kind) for r in sec.get("results") or []) if c]
    return {"kind": "artist", "id": browse_id, "title": a.get("name") or "", "thumb": _thumb(a.get("thumbnails")),
            "subscribers": a.get("subscribers") or "", "songs": part("songs", "song"),
            "albums": part("albums", "album") + part("singles", "album"),
            "songs_list": ((a.get("songs") or {}).get("browseId") or "")}


def album(browse_id: str, hourly: int = HOURLY) -> dict:
    a = _ask(("album", browse_id), PAGE_TTL, hourly, lambda: _yt().get_album(browse_id))
    cover = _thumb(a.get("thumbnails"))
    tracks = []
    for t in a.get("tracks") or []:
        c = card({**t, "album": {"name": a.get("title")}, "thumbnails": t.get("thumbnails") or a.get("thumbnails")}, "song")
        if c:
            c["thumb"] = c["thumb"] or cover
            tracks.append(c)
    return {"kind": "album", "id": browse_id, "title": a.get("title") or "", "artist": _artists(a.get("artists")),
            "type": a.get("type") or "Álbum", "year": a.get("year") or "", "thumb": cover,
            "playlist": a.get("audioPlaylistId") or "", "tracks": tracks}


def playlist(playlist_id: str, hourly: int = HOURLY) -> dict:
    p = _ask(("playlist", playlist_id), PAGE_TTL, hourly, lambda: _yt().get_playlist(playlist_id, limit=300))
    tracks = [c for c in (card(t, "song") for t in p.get("tracks") or []) if c]
    return {"kind": "playlist", "id": playlist_id, "title": p.get("title") or "", "artist": _artists(p.get("author")),
            "thumb": _thumb(p.get("thumbnails")), "count": p.get("trackCount") or len(tracks), "tracks": tracks}


def link(item: dict) -> str:
    """What the queue downloads for a card: a song, or an album's / playlist's whole list."""
    kind, iid = item.get("kind"), str(item.get("id") or "")
    if kind == "song":
        return f"https://music.youtube.com/watch?v={iid}"
    if kind == "album":
        pl = item.get("playlist") or album(iid)["playlist"]
        return f"https://music.youtube.com/playlist?list={pl}" if pl else ""
    if kind == "playlist":
        return f"https://music.youtube.com/playlist?list={iid}"
    return ""


# ---------------------------------------------------------------- covers

def _thumbs_dir() -> Path:
    return app_dir() / "cache" / "catalog-thumbs"


def thumbs(urls: list[str], session=None) -> dict[str, str]:
    """{url: data URI} for covers (the page loads no remote images); kept on disk, fetched six at
    a time, only from YouTube's image hosts. A cover that fails is simply missing."""
    from concurrent.futures import ThreadPoolExecutor
    from urllib.parse import urlparse

    def one(url: str) -> tuple[str, str]:
        host = urlparse(url).hostname or ""
        if not url.startswith("https://") or not host.endswith(THUMB_HOSTS):
            return url, ""
        f = _thumbs_dir() / (hashlib.sha1(url.encode()).hexdigest() + ".jpg")
        try:
            if f.is_file():
                return url, "data:image/jpeg;base64," + base64.b64encode(f.read_bytes()).decode()
            import requests
            from PIL import Image
            get = (session or requests).get
            r = get(url, timeout=10)
            if r.status_code != 200 or len(r.content) > 2_000_000:
                return url, ""
            with Image.open(io.BytesIO(r.content)) as im:
                im = im.convert("RGB")
                im.thumbnail((300, 300))
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=84)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(buf.getvalue())
            return url, "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        except Exception:
            return url, ""

    want = list(dict.fromkeys(u for u in urls if u))[:80]
    with ThreadPoolExecutor(max_workers=6) as pool:
        return {u: d for u, d in pool.map(one, want) if d}
