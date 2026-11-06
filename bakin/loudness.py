"""Bakin Música: loudness of each song, for «Volumen parejo» (PLAN 0.16.1).

Each file is measured once with ffmpeg's ebur128 filter (integrated
loudness in LUFS and true peak in dBFS, EBU R128) and kept in Bakin's
private data folder (music/loudness.json) keyed by library id, with the
file's size and modification time so a changed file is measured again.
From those numbers the player gets a ReplayGain-2.0-style gain per song
(track or album mode) that never makes a song clip.

ffmpeg runs one file at a time at below-normal priority, so measuring a
large library never competes with playback.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from . import tools
from .music import main_artist
from .paths import app_dir

BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
TIMEOUT = 120.0                 # seconds per file
TARGET = -18.0                  # LUFS (ReplayGain 2.0 reference)
HEADROOM = -1.0                 # peak + gain never above this (dBFS)
SILENCE = -60.0                 # tracks quieter than this don't count for the album
MIN_PEAK = -120.0               # «-inf dBFS» (digital silence) is stored as this
SAVE_EVERY = 10

_lock = threading.Lock()

_SUMMARY = re.compile(r"Summary:\s*$", re.M)
_I = re.compile(r"^\s*I:\s*(-?\d+(?:\.\d+)?|-?inf)\s*LUFS", re.M | re.I)
_LRA = re.compile(r"^\s*LRA:\s*(\d+(?:\.\d+)?)\s*LU\s*$", re.M | re.I)
_PEAK = re.compile(r"True peak:\s*\n\s*Peak:\s*(-?\d+(?:\.\d+)?|-?inf)\s*dBFS", re.I)


def _path() -> Path:
    return app_dir() / "music" / "loudness.json"


def load() -> dict:
    """{item_id: {"lufs", "peak_db", "size", "mtime"}}; {} if missing or unreadable."""
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=p.stem, suffix=".tmp", dir=p.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False)
    os.replace(tmp, p)


def _num(s: str, floor: float) -> float:
    v = float(s)            # float() reads «-inf» too
    return max(v, floor) if not math.isnan(v) else floor


def parse(stderr: str) -> dict | None:
    """The summary ffmpeg's ebur128 filter prints at the end → {"lufs", "peak_db"}."""
    m = None
    for m in _SUMMARY.finditer(stderr or ""):
        pass
    if m is None:
        return None
    tail = stderr[m.end():]
    i, p = _I.search(tail), _PEAK.search(tail)
    if not i or not p:
        return None
    out = {"lufs": _num(i.group(1), -70.0), "peak_db": _num(p.group(1), MIN_PEAK)}
    lra = _LRA.search(tail)
    if lra:  # loudness range: how much it moves between quiet and loud parts (0.20.3)
        out["lra"] = float(lra.group(1))
    return out


def measure(path: Path, cancel: threading.Event | None = None) -> dict | None:
    """Integrated loudness and true peak of the first audio stream of `path`.

    {"lufs": float, "peak_db": float}, or None if ffmpeg is missing, fails,
    takes longer than TIMEOUT or `cancel` is set (ffmpeg is then killed).
    Silence measures -70 LUFS and is returned as such."""
    ffmpeg = tools.find("ffmpeg")
    if ffmpeg is None:
        return None
    cmd = [str(ffmpeg), "-nostdin", "-hide_banner", "-nostats", "-i", str(path), "-map", "0:a:0",
           "-af", "ebur128=peak=true:framelog=quiet", "-f", "null", "-"]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                env=tools.child_env(),
                                creationflags=tools.CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS)
    except OSError:
        return None
    deadline = time.monotonic() + TIMEOUT
    while True:
        try:
            _, err = proc.communicate(timeout=0.25)     # may be retried after TimeoutExpired
            break
        except subprocess.TimeoutExpired:
            if (cancel is not None and cancel.is_set()) or time.monotonic() > deadline:
                proc.kill()
                try:
                    proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                return None
    if proc.returncode != 0:
        return None
    return parse(err.decode("utf-8", "replace"))


def _stale(entry: dict | None, item: dict) -> bool:
    return (not entry or "lufs" not in entry
            or entry.get("size") != item.get("size") or entry.get("mtime") != item.get("mtime"))


def scan(items: list[dict], cancel: threading.Event,
         progress: Callable[[int, int], None] = lambda done, total: None, *, prune: bool = False) -> dict:
    """Measure the songs that have no measurement or whose file changed (size/mtime).

    Items are library songs ({"id", "path", "size", "mtime", ...}). One file
    at a time; saved every SAVE_EVERY files and at the end. Stops between
    (or during) files when `cancel` is set. `progress(done, total)` counts
    only the files to measure. With prune=True, entries for ids no longer
    in `items` are dropped. → {"measured", "skipped", "failed"}"""
    with _lock:
        data = load()
        if prune:
            ids = {it.get("id") for it in items}
            data = {k: v for k, v in data.items() if k in ids}
        todo = [it for it in items if it.get("id") and it.get("path") and _stale(data.get(it["id"]), it)]
        skipped = len(items) - len(todo)
        measured = failed = done = 0
        progress(0, len(todo))
        dirty = prune
        for it in todo:
            if cancel.is_set():
                break
            r = measure(Path(it["path"]), cancel)
            if cancel.is_set():
                break
            if r is None:
                failed += 1
            else:
                data[it["id"]] = {"lufs": round(r["lufs"], 2), "peak_db": round(r["peak_db"], 2),
                                  "size": it.get("size"), "mtime": it.get("mtime")}
                if "lra" in r:
                    data[it["id"]]["lra"] = round(r["lra"], 1)
                measured += 1
                dirty = True
            done += 1
            progress(done, len(todo))
            if dirty and done % SAVE_EVERY == 0:
                _save(data)
                dirty = False
        if dirty:
            _save(data)
    return {"measured": measured, "skipped": skipped, "failed": failed}


def album_loudness(entries: list[dict]) -> float | None:
    """An album's integrated loudness from its tracks' (energy average,
    tracks under SILENCE ignored); None if no track counts."""
    vals = [e["lufs"] for e in entries if isinstance(e, dict) and isinstance(e.get("lufs"), (int, float))
            and e["lufs"] >= SILENCE]
    if not vals:
        return None
    return 10 * math.log10(sum(10 ** (v / 10) for v in vals) / len(vals))


def energy(entry: dict | None) -> float | None:
    """How intense a song sounds, 0 (calm) … 1 (intense), from its measurement (PLAN 0.20.3):
    louder and more even (little loudness range) reads as intense; quiet with a wide range as
    calm. Cheap and license-free (no bliss); None for silence or no measurement."""
    lufs = entry.get("lufs") if isinstance(entry, dict) else None
    if not isinstance(lufs, (int, float)) or lufs < SILENCE:
        return None
    e = (lufs + 30.0) / 24.0                       # -30 LUFS → 0, -6 LUFS → 1
    lra = entry.get("lra")
    if isinstance(lra, (int, float)):
        e -= (lra - 7.0) / 28.0                    # a wide range pulls toward calm
    return max(0.0, min(1.0, e))


def gain_db(entry: dict, mode: str = "track", preamp: float = 0.0, album_lufs: float | None = None,
            target: float = TARGET) -> float:
    """Gain in dB toward `target` LUFS (+ preamp), limited so the true peak
    stays at or below HEADROOM. 0.0 for an entry without a measurement."""
    lufs = entry.get("lufs") if isinstance(entry, dict) else None
    if not isinstance(lufs, (int, float)):
        return 0.0
    ref = album_lufs if mode == "album" and album_lufs is not None else lufs
    gain = target - ref + preamp
    peak = entry.get("peak_db")
    if isinstance(peak, (int, float)):
        gain = min(gain, HEADROOM - peak)
    return round(gain, 2)


def _album_key(it: dict) -> tuple[str, str] | None:
    album = (it.get("album") or "").strip().casefold()
    return (album, main_artist(it.get("artist") or "")) if album else None


def gains_for(items: list[dict], mode: str, preamp: float, data: dict | None = None) -> dict[str, float]:
    """{item_id: gain_db} for the items that have a measurement. In album mode
    songs are grouped by (album, main artist); a song without album uses its
    own loudness. `data` defaults to load()."""
    data = load() if data is None else data
    albums: dict[tuple[str, str], float | None] = {}
    if mode == "album":
        groups: dict[tuple[str, str], list[dict]] = {}
        for it in items:
            key, e = _album_key(it), data.get(it.get("id"))
            if key and e:
                groups.setdefault(key, []).append(e)
        albums = {k: album_loudness(v) for k, v in groups.items()}
    out = {}
    for it in items:
        e = data.get(it.get("id"))
        if not e or "lufs" not in e:
            continue
        key = _album_key(it) if mode == "album" else None
        alb = albums.get(key) if key else None
        out[it["id"]] = gain_db(e, "album" if alb is not None else "track", preamp, alb)
    return out
