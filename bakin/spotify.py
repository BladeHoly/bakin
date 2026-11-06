"""A Spotify link's songs, to find on YouTube Music (PLAN 0.15.2). Nothing comes from Spotify but
names and lengths: the public embed page of a track, an album or a playlist (no account, one
request per link; a playlist shows its first 100 songs), or a CSV the user exported themselves
(Exportify's columns) for longer ones. The audio is never Spotify's.
"""

from __future__ import annotations

import csv
import io
import json
import re

LINK = re.compile(r"(?:open\.spotify\.com/(?:intl-[a-z-]+/)?(?:embed/)?|spotify:)(track|album|playlist)[/:]([A-Za-z0-9]{22})")
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


class SpotifyError(Exception):
    """Something to show the user (Spanish)."""


def parse_link(text: str) -> tuple[str, str] | None:
    """(kind, id) of a Spotify link or URI, or None."""
    m = LINK.search(str(text or ""))
    return (m.group(1), m.group(2)) if m else None


def is_link(text: str) -> bool:
    return parse_link(text) is not None


def _track(title: str, artists: list[str], duration_ms, explicit=False, album: str = "") -> dict:
    return {"title": str(title or "").strip(), "artists": [a for a in (str(x).strip() for x in artists) if a],
            "duration": round(float(duration_ms) / 1000, 1) if duration_ms else None,
            "explicit": bool(explicit), "album": album}


def from_page(html: str) -> dict:
    """{"kind", "name", "tracks"} from an embed page's data."""
    m = NEXT_DATA.search(html or "")
    if not m:
        raise SpotifyError("Spotify no devolvió la lista (¿el enlace es privado?).")
    try:
        e = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    except (ValueError, KeyError, TypeError) as exc:
        raise SpotifyError("Spotify cambió su página: actualiza Bakin.") from exc
    kind = e.get("type")
    if kind == "track":
        return {"kind": "track", "name": e.get("name") or "",
                "tracks": [_track(e.get("name"), [a.get("name") for a in e.get("artists") or []],
                                  e.get("duration"), e.get("isExplicit"))]}
    album = e.get("name") or "" if kind == "album" else ""
    tracks = [_track(t.get("title"), str(t.get("subtitle") or "").split(", "), t.get("duration"),
                     t.get("isExplicit"), album) for t in e.get("trackList") or []]
    return {"kind": kind or "playlist", "name": e.get("name") or "", "tracks": [t for t in tracks if t["title"]]}


def fetch(text: str, session=None) -> dict:
    """The songs behind a Spotify link (its public embed page; one request)."""
    parsed = parse_link(text)
    if not parsed:
        raise SpotifyError("Ese no es un enlace de Spotify.")
    kind, sid = parsed
    import os
    fake = os.environ.get("BAKIN_FAKE_SPOTIFY")
    if fake and os.environ.get("BAKIN_HOME"):  # the UI test: a saved page, never the network
        from pathlib import Path
        out = from_page(Path(fake).read_text(encoding="utf-8"))
        return {**out, "limited": False}
    import requests
    get = (session or requests).get
    try:
        r = get(f"https://open.spotify.com/embed/{kind}/{sid}", timeout=15,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept-Language": "es"})
    except requests.RequestException as exc:
        raise SpotifyError("No se pudo conectar con Spotify.") from exc
    if r.status_code == 404:
        raise SpotifyError("Spotify no encuentra ese enlace (¿es privado?).")
    if r.status_code in (429, 503):
        raise SpotifyError("Spotify pidió esperar: prueba en un rato.")
    if r.status_code != 200:
        raise SpotifyError(f"Spotify respondió con un error ({r.status_code}).")
    out = from_page(r.text)
    out["limited"] = out["kind"] == "playlist" and len(out["tracks"]) >= 100
    return out


def from_csv(text: str) -> dict:
    """The songs of a CSV exported from Spotify (Exportify's columns, or any with track name,
    artist name(s) and duration in ms)."""
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    if not rows:
        raise SpotifyError("El archivo no tiene temas.")
    keys = {k.lower().strip(): k for k in rows[0].keys() if k}

    def col(*names):
        return next((keys[n] for n in names if n in keys), None)
    title, artist = col("track name", "name", "title"), col("artist name(s)", "artist names", "artist", "artists")
    dur, album = col("duration (ms)", "duration_ms", "duration"), col("album name", "album")
    if not title or not artist:
        raise SpotifyError("No reconozco las columnas: hace falta el nombre del tema y del artista.")
    tracks = []
    for r in rows[:5000]:
        artists = re.split(r"\s*[,;]\s*", r.get(artist) or "")
        tracks.append(_track(r.get(title), artists, (r.get(dur) or "").strip() or None, False, (r.get(album) or "") if album else ""))
    return {"kind": "playlist", "name": "", "tracks": [t for t in tracks if t["title"]], "limited": False}
