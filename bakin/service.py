"""UI-agnostic application service: settings, jobs and their events.

Front ends (the Tauri shell through bakin.sidecar, tests) call these methods
and receive progress through the `push` callable they inject.

Security model:
* every argument coming from a UI is validated here;
* `open_*` only opens paths produced by the current job, inside its folder.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import threading
import time
import webbrowser
from pathlib import Path
from typing import Callable

from . import __version__, redact
from .paths import app_dir, is_inside, log_file, user_downloads_dir

log = logging.getLogger("bakin.service")
THUMB_PX = 420

DEFAULT_SETTINGS = {"out_dir": str(user_downloads_dir()), "wallhaven_user": "",
                    "musicbrainz": False,  # PLAN 0.14.2: ask MusicBrainz (opt-in; sends title, artist, length)
                    "catalog_hourly": 100,  # PLAN 0.15: YouTube Music catalogue requests an hour (0 = no limit)
                    "catalog_covers": True,  # PLAN 0.15.5: the catalogue's covers (one small image each)
                    "loudness_mode": "off",  # PLAN 0.16.1: even volume: off | track | album
                    "loudness_preamp": 0.0,  # dB on top (0…6)
                    "lyrics_online": False,  # PLAN 0.16.3: ask LRCLIB (sends title, artist, album, length)
                    "lyrics_beside": True,  # keep a song's .lrc next to it (other players read it too)
                    "lan_enabled": False,  # PLAN 0.21.1: share the music on the home network (the shell serves it)
                    "lan_port": 4533,
                    "lang": "es"}  # PLAN 0.22.3: the interface's language: es | en | auto (Windows' language)
QUEUE_LIMIT = 2000   # a whole music library can be queued for better copies
LANES = ("media", "wallhaven", "convert")


def _lanes_for(scope: str) -> tuple:
    """The lanes a pause holds: one, "downloads" (music and wallpapers: the download hours), or all."""
    return ("media", "wallhaven") if scope == "downloads" else (scope,) if scope in LANES else LANES
LANE_NAMES = {"media": "Música y video", "wallhaven": "Fondos de Wallhaven", "convert": "Conversiones"}
WALLHAVEN_LIMIT = 200  # Wallhaven's lane: its own count, ahead of the music


def _settings_path() -> Path:
    return app_dir() / "ui-settings.json"


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    try:
        s.update({k: v for k, v in json.loads(_settings_path().read_text(encoding="utf-8")).items()
                  if k in DEFAULT_SETTINGS})
    except Exception:
        pass
    return s


def save_settings(s: dict) -> None:
    try:
        from . import migrate  # the version stays, so the next start has nothing to migrate
        _settings_path().write_text(json.dumps(migrate.stamp("ui-settings.json", {k: s[k] for k in DEFAULT_SETTINGS}), indent=1),
                                    encoding="utf-8")
    except OSError:
        pass


def make_thumb(path: Path, size: int = THUMB_PX) -> str:
    """Small JPEG preview as a data URI. Reads the file; never modifies it."""
    from PIL import Image
    with Image.open(path) as im:
        im.draft("RGB", (size * 2, size * 2))
        im = im.convert("RGB")
        im.thumbnail((size, size))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82)  # in-memory preview only
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


LIBRARY_PAGE = 24
COVER_PX = 240


def _tools_checked(name: str) -> float:
    """When Bakin last checked a tool for updates (unix time; 0 if never)."""
    import calendar
    import time as _t
    from . import tools
    try:
        stamp = tools._state().get(name, {}).get("checked_at")
        return float(calendar.timegm(_t.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))) if stamp else 0.0
    except (ValueError, TypeError, OSError):
        return 0.0


def _library_root(out_dir: str | None) -> Path:
    return Path(out_dir or load_settings()["out_dir"]).expanduser().resolve()


def _remote_thumb(url) -> str:
    """A video's thumbnail as a small data URI, so the page never loads
    remote images (its CSP allows none)."""
    if not url or not str(url).startswith("https://"):
        return ""
    try:
        import requests
        from PIL import Image
        with requests.get(str(url), timeout=10, stream=True) as r:
            r.raise_for_status()
            data = r.raw.read(3 * 1024 * 1024 + 1, decode_content=True)
        if len(data) > 3 * 1024 * 1024:
            return ""
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
            im.thumbnail((480, 480))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=82)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""


def tools_missing_ffmpeg() -> bool:
    from . import tools
    return tools.find("ffmpeg") is None


def _is_web_url(url) -> bool:
    return bool(re.match(r"^https?://[^\s/$.?#].[^\s]*$", str(url or "").strip(), re.I))


class Service:
    """The app's operations. Progress goes out through `push(payload)`."""

    # what a used Bakin always has one of; none of them → the guided first start (PLAN 0.22.2)
    USED_MARKS = ("ui-settings.json", "media.json", "queue.json", "follows.json", "music")

    def __init__(self, push: Callable[[dict], None] | None = None):
        self._push_fn = push
        self._first_run = not any((app_dir() / m).exists() for m in self.USED_MARKS)
        self._worker: threading.Thread | None = None
        self._cancel = threading.Event()
        self._photos: dict[str, Path] = {}
        self._job_dir: Path | None = None
        self._lock = threading.Lock()
        # Jobs run one after another; a stop (429, a bot check...) pauses the rest.
        self._queue: list[dict] = []
        self._current: dict | None = None
        # Each lane of the queue waits on its own: {lane: {"until": unix time or None, "reason"}}
        # (the daily limit holds music, Wallhaven's 429 holds wallpapers, the user any or all).
        self._holds: dict[str, dict] = {}
        self._next_id = 0
        self._dirty = False                      # queue changed since it was last saved
        self._closing = False
        self._qfile = app_dir() / "queue.json"   # fixed at start: the data folder doesn't move under us
        self._moving = False
        self._tools_busy = False
        try:  # plain cookie copies a crash left behind (vault.py)
            from . import vault
            vault.sweep()
        except OSError:
            pass
        self._restore_queue()
        threading.Thread(target=self._scheduler, name="bakin-scheduler", daemon=True).start()
        self._lib_lock = threading.Lock()
        self._lib_cache: dict | None = None   # {"root", "cols", "items": {col_id: [...]}}
        self._mb_cancel = threading.Event()   # PLAN 0.14.2: the MusicBrainz lookup running
        self._mb_busy = False
        self._mb_covers: dict[str, bytes] = {}
        self._mirror_timers: dict[str, threading.Timer] = {}  # PLAN 0.20.1: a playlist's sync, soon
        self._mirror_lock = threading.Lock()
        self._mirror_busy = threading.Lock()  # one sync at a time: they share the folder's manifest  # release mbid → cover, until the user applies
        self._sp_cancel = threading.Event()   # PLAN 0.15.2: a Spotify list being matched
        self._sp_busy = False
        self._loud_cancel = threading.Event()  # PLAN 0.16.1: the loudness scan
        self._loud_busy = False

    def _push(self, payload: dict) -> None:
        if self._push_fn is None:
            return
        try:
            self._push_fn(payload)
        except Exception as exc:  # UI gone
            log.debug("push failed: %s", exc.__class__.__name__)

    # --------------------------------------------------------------- state

    # ------------------------------------------------ persistent queue
    # The queue is saved to disk (at most every half minute while it changes,
    # and on close), so closing Bakin or restarting Windows doesn't lose it.

    def _queue_file(self) -> Path:
        return self._qfile

    def _serialize(self, item: dict) -> dict | None:
        kind = item.get("kind")
        out = {"kind": kind, "title": item["title"], "mode": item["mode"]}
        if kind == "media":
            out.update(url=item["url"], preset=item["preset"], root=str(item["root"]),
                       retry_on_partial=item["retry_on_partial"], replaces=item.get("replaces"),
                       counts=item.get("counts", True))
        elif kind == "expand":
            out.update(url=item["url"], list_preset=item["list_preset"], child_preset=item["child_preset"],
                       root=str(item["root"]))
        elif kind == "convert":
            out.update(files=[str(f) for f in item["files"]], opts=item["opts"])
        elif kind == "wallhaven":
            out.update(job=item["job"], root=str(item["root"]))
        else:
            return None
        return out

    def _deserialize(self, d: dict) -> dict | None:
        self._next_id += 1
        base = {"id": self._next_id, "kind": d.get("kind"), "title": d.get("title", ""),
                "mode": d.get("mode", "download"), "opts": d.get("opts") or {"max_posts": 0}}
        kind = base["kind"]
        if kind == "media":
            base.update(url=d["url"], preset=d["preset"], root=Path(d["root"]),
                        retry_on_partial=bool(d.get("retry_on_partial")), replaces=d.get("replaces"),
                        counts=d.get("counts", True))
        elif kind == "expand":
            base.update(url=d["url"], list_preset=d["list_preset"], child_preset=d["child_preset"],
                        root=Path(d["root"]))
        elif kind == "convert":
            base.update(files=[Path(f) for f in d["files"] if Path(f).is_file()])
            if not base["files"]:
                return None
        elif kind == "wallhaven":
            if not isinstance(d.get("job"), dict) or not d.get("root"):
                return None
            base.update(job=d["job"], root=Path(d["root"]))
        else:
            return None  # jobs of kinds older versions ran and this one doesn't
        return base

    def _restore_queue(self) -> None:
        try:
            data = json.loads(self._queue_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        items = [x for x in (self._deserialize(d) for d in data.get("items", []) if isinstance(d, dict)) if x]
        with self._lock:
            self._queue = items
            holds = data.get("holds")
            if isinstance(holds, dict):
                self._holds = {k: {"until": v.get("until"), "reason": str(v.get("reason") or "")}
                               for k, v in holds.items() if k in LANES and isinstance(v, dict)}
            elif data.get("paused"):  # saved by 0.13 and before: one pause, maybe for music only
                scope = data.get("scope")
                self._hold(LANES if scope not in LANES else (scope,), str(data.get("reason") or ""), data.get("paused_until"))
            if self._queue:
                self._ensure_worker()

    def _save_queue(self, force: bool = False) -> None:
        with self._lock:
            if not (self._dirty or force):
                return
            items = ([self._current] if self._current else []) + list(self._queue)
            data = {"items": [x for x in (self._serialize(i) for i in items) if x],
                    "holds": self._holds}
            self._dirty = False
        path = self._queue_file()
        try:
            tmp = path.with_suffix(".tmp")
            from . import migrate
            tmp.write_text(json.dumps(migrate.stamp("queue.json", data), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("could not save the queue: %s", exc)

    def shutdown(self) -> None:
        """The app is closing: stop the current job but keep it, and the rest
        of the queue, for next time."""
        with self._lock:
            self._closing = True
            if self._current:
                self._queue.insert(0, self._current)
                self._current = None
            self._dirty = True
            self._cancel.set()
        self._save_queue(force=True)

    def _scheduler(self) -> None:
        """Lift timed pauses (daily limit, YouTube pushback) and save the queue."""
        import time as _t
        self.mirror_all_soon()  # mirrored playlists catch up a little after start (0.20.1)
        while not self._closing:
            _t.sleep(15)
            self._tick()

    def _tick(self, now: float | None = None) -> bool:
        """One scheduler step; True if a timed pause was lifted."""
        import time as _t
        resumed = False
        with self._lock:
            if self._closing:
                return False
            t0 = now or _t.time()
            over = [lane for lane, h in self._holds.items() if h.get("until") and t0 >= h["until"]]
            for lane in over:
                del self._holds[lane]
            if over:
                self._dirty = True
                self._ensure_worker()
                resumed = True
        if resumed:
            self._push({"kind": "info", "message": "Se reanuda la cola sola, como estaba programado."})
            self._push_queue()
        t = now or _t.time()
        # yt-dlp once a day (the date is kept on disk: the engine sleeps and wakes often)
        if self._current is None and (getattr(self, "_ytdlp_force", False)
                                      or t - max(getattr(self, "_ytdlp_checked_at", 0), _tools_checked("yt-dlp")) >= 24 * 3600):
            self._ytdlp_force = False
            self._ytdlp_checked_at = t
            self.media_maintenance()
        if t - getattr(self, "_follows_checked", 0) >= 300:
            self._follows_checked = t
            try:
                n = self._check_follows(t)
            except Exception as exc:  # a follow must never stop the scheduler
                log.warning("follow check failed: %s", exc.__class__.__name__)
                n = 0
            if n:
                self._push({"kind": "info", "message": f"Revisando {n} seguido{'s' if n != 1 else ''}: lo nuevo se suma a la cola."})
        self._save_queue()
        return resumed

    def pause_queue(self, reason: str = "Pausada por ti.", until: float | None = None, scope: str = "all") -> dict:
        """Pause the queue. `scope` "media": only music/video wait (YouTube's daily
        limit or pushback) and Wallhaven and conversions go on; "wallhaven": only
        Wallhaven waits (its 429); "all": everything (the user, the download hours)."""
        with self._lock:
            self._hold(_lanes_for(scope), reason, until)
            self._ensure_worker()  # with a partial pause, the rest of the queue goes on
        self._push_queue()
        return {"ok": True}

    # ------------------------------------------------------------ lanes
    # One queue, three lanes: music and video, wallpapers, conversions. A lane
    # that waits (a pause) never holds the others.

    @staticmethod
    def _lane_of(item: dict) -> str:
        k = item.get("kind")
        return "media" if k in ("media", "expand") else "wallhaven" if k == "wallhaven" else "convert"

    def _hold(self, lanes, reason: str, until: float | None) -> None:
        for lane in lanes:
            self._holds[lane] = {"until": until, "reason": reason}
        self._dirty = True

    # The old single-pause view of it, for the code and tests that ask «is the queue paused?»
    @property
    def _paused(self) -> bool:
        return bool(self._holds)

    @_paused.setter
    def _paused(self, value: bool) -> None:
        if not value:
            self._holds.clear()
        elif not self._holds:
            self._hold(LANES, "", None)

    @property
    def _paused_until(self) -> float | None:
        times = [h["until"] for h in self._holds.values() if h.get("until")]
        return min(times) if times else None

    @_paused_until.setter
    def _paused_until(self, value: float | None) -> None:
        for h in self._holds.values():
            h["until"] = value

    @property
    def _pause_reason(self) -> str:
        h = self._holds.get("media") or next(iter(self._holds.values()), None)
        return (h or {}).get("reason", "")

    @_pause_reason.setter
    def _pause_reason(self, value: str) -> None:
        for h in self._holds.values():
            h["reason"] = value

    @property
    def _pause_scope(self) -> str:
        return "all" if set(self._holds) >= set(LANES) else next(iter(self._holds), "all")

    @_pause_scope.setter
    def _pause_scope(self, scope: str) -> None:
        keep = next(iter(self._holds.values()), {"until": None, "reason": ""})
        self._holds = {lane: dict(keep) for lane in (LANES if scope not in LANES else (scope,))} if self._holds else {}

    def _runnable(self, item: dict) -> bool:
        return self._lane_of(item) not in self._holds

    def queue_pause(self, lane: str = "") -> dict:
        """«Pausar» on one lane of Descargas (or all of them)."""
        with self._lock:
            self._hold([lane] if lane in LANES else LANES, "Pausada por ti.", None)
        self._push_queue()
        return {"ok": True}

    def queue_resume(self, lane: str = "") -> dict:
        """«Reanudar» on one lane (or all of them): also lifts a timed wait."""
        with self._lock:
            for x in ([lane] if lane in LANES else list(LANES)):
                self._holds.pop(x, None)
            self._dirty = True
            self._ensure_worker()
        self._push_queue()
        return {"ok": True}

    def _next_index(self) -> int | None:
        """The first item of the queue that may run now. Hold self._lock."""
        return next((i for i, it in enumerate(self._queue) if self._runnable(it)), None)

    # ------------------------------------------------- daily budget
    # A daily limit for music/video downloads keeps YouTube from flagging the
    # connection; the counter lives with the media settings.

    # ------------------------------------------------------- engine on demand
    # With the window closed and nothing to do, the shell lets the engine go
    # (PLAN 0.12.1): it asks first, and wakes it again at `wake_at` (a timed
    # pause ending, a follow coming due) or whenever something needs it.

    def sleep_if_idle(self, now: float | None = None) -> dict:
        """{"idle": True, "wake_at": unix time or None} when nothing is running
        or waiting to run (the queue is saved first); {"idle": False, "why"}
        otherwise."""
        import time as _t
        t = now or _t.time()
        with self._lock:
            if self._current is not None or (self._worker is not None and self._worker.is_alive()):
                return {"idle": False, "why": "hay una descarga en curso"}
            if self._moving or self._tools_busy:
                return {"idle": False, "why": "está moviendo carpetas o instalando herramientas"}
            if self._queue and self._next_index() is not None:
                return {"idle": False, "why": "hay trabajos en la cola"}
            wake = [self._paused_until] if (self._paused and self._paused_until) else []
        for f in self._load_follows():
            if f.get("kind") in ("media", "wallhaven") and f.get("enabled", True):
                wake.append(float(f.get("last_check", 0) or 0) + float(f.get("every_hours", 24) or 24) * 3600)
        wake_at = min(wake) if wake else None
        if wake_at is not None and wake_at <= t + 5:
            return {"idle": False, "why": "hay algo por revisar ahora"}
        self._save_queue(force=True)
        return {"idle": True, "wake_at": wake_at}

    def resume_due(self) -> dict:
        """Called by the shell when it wakes the engine at `wake_at`: lift a
        pause that is over and check the follows that are due, now."""
        self._follows_checked = 0
        self._tick()
        return {"ok": True}

    def _budget(self) -> tuple[int, int]:
        """(limit, downloads today); limit 0 = none."""
        from . import mediadl
        import time as _t
        ms = mediadl.load_settings()
        today = _t.strftime("%Y-%m-%d")
        used = ms.get("downloads_today", {})
        return ms.get("daily_limit", 0), (used.get("count", 0) if used.get("date") == today else 0)

    def _count_downloads(self, n: int) -> None:
        if not n:
            return
        from . import mediadl
        import time as _t
        ms = mediadl.load_settings()
        today = _t.strftime("%Y-%m-%d")
        used = ms.get("downloads_today", {})
        count = (used.get("count", 0) if used.get("date") == today else 0) + n
        mediadl.save_settings({**ms, "downloads_today": {"date": today, "count": count}})

    def _outside_hours(self, now=None) -> tuple[str, float] | None:
        """(reason, resume time) if downloads are limited to some hours and
        it's outside them; None otherwise."""
        import datetime as _dt
        from . import mediadl
        h = mediadl.load_settings()["hours"]
        if not h["enabled"] or h["from"] == h["to"]:
            return None
        now = now or _dt.datetime.now()
        a, b, cur = h["from"], h["to"], now.hour
        inside = (a <= cur < b) if a < b else (cur >= a or cur < b)  # a window may cross midnight
        if inside:
            return None
        start = now.replace(hour=a, minute=0, second=0, microsecond=0)
        if start <= now:
            start += _dt.timedelta(days=1)
        return (f"Fuera del horario de descargas ({a:02d}:00–{b:02d}:00). Empieza sola a las {a:02d}:00.",
                start.timestamp())

    def set_hours(self, enabled: bool, start: int = 1, end: int = 8) -> dict:
        from . import mediadl
        ms = mediadl.load_settings()
        try:
            ms["hours"] = {"enabled": bool(enabled), "from": max(0, min(23, int(start))), "to": max(0, min(23, int(end)))}
        except (TypeError, ValueError):
            return {"ok": False, "error": "hora inválida"}
        mediadl.save_settings(ms)
        if not enabled:
            with self._lock:
                outside = self._paused and self._pause_reason.startswith("Fuera del horario")
            if outside:
                self.resume_queue()
        return {"ok": True, "hours": ms["hours"]}

    # --------------------------------------------------------------- cookies
    # Whether the presets' cookies still log in: the file's own data (never
    # its values) plus what the last downloads saw. A newer file clears it.

    def _cookies_health_file(self) -> Path:
        return self._qfile.with_name("cookies-health.json")

    def _cookies_health(self) -> dict:
        try:
            data = json.loads(self._cookies_health_file().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _cookies_key(preset: dict) -> str:
        if preset["cookies_source"] == "vault":
            return "vault:youtube"
        if preset["cookies_source"] == "file":
            return str(Path(preset["cookies_file"]).expanduser())
        return "browser:" + preset["cookies_browser"]

    def _mark_cookies_bad(self, preset: dict) -> None:
        import time as _t
        key = self._cookies_key(preset)
        health = self._cookies_health()
        prev = health.get(key)
        try:  # a file exported after the last failure counts as new cookies
            # file times use a coarser clock than time.time(): a second of slack
            from . import mediadl, vault
            when = vault.mtime(mediadl.VAULT_COOKIES) if key == "vault:youtube" else Path(key).stat().st_mtime
            renewed = prev is not None and when + 1 >= prev["bad_since"]
        except (OSError, ValueError, KeyError, TypeError):
            renewed = False
        first = prev is None or renewed
        health[key] = {"bad_since": _t.time()}
        p = self._cookies_health_file()
        try:
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(health, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, p)
        except OSError:
            pass
        if first:
            self._push({"kind": "cookies_bad", "file": key, "preset": preset["name"]})

    def cookies_status(self) -> dict:
        from . import mediadl
        ms = mediadl.load_settings()
        health = self._cookies_health()
        by_key: dict[str, dict] = {}
        for p in ms["presets"]:
            if p["cookies_mode"] == "off":
                continue
            key = self._cookies_key(p)
            entry = by_key.get(key)
            if entry is None:
                if p["cookies_source"] == "vault":
                    entry = self._vault_cookies_status()
                elif p["cookies_source"] == "file":
                    entry = mediadl.cookies_file_status(key)
                else:
                    entry = {"file": key, "browser": p["cookies_browser"], "exists": True}
                entry["presets"] = []
                bad = health.get(key)
                # a file saved after the failure is a new export: give it a chance
                entry["bad"] = bool(bad) and not (entry.get("mtime") and entry["mtime"] + 1 >= bad["bad_since"])
                by_key[key] = entry
            entry["presets"].append(p["name"])
        return {"cookies": list(by_key.values())}

    # ---------------------------------------------------- cookies in the vault
    # (2026-11-07, the user: «convertímelas así las puedo borrar de descargas») A cookies.txt
    # is imported once into Bakin's vault (vault.py: encrypted for this Windows user) and the
    # presets that used it use the vault; the plain file can then be deleted from Bakin.

    @staticmethod
    def _vault_cookies_status() -> dict:
        from . import mediadl, vault
        out = {"file": "vault:youtube", "vault": True, "exists": False, "count": 0, "youtube_session": False,
               "expires": None, "expired": False, "mtime": None}
        try:
            data = vault.get(mediadl.VAULT_COOKIES)
        except OSError:
            return {**out, "unreadable": True}
        if data is None:
            return out
        mt = vault.mtime(mediadl.VAULT_COOKIES)
        return {**out, **mediadl.cookies_text_status(data.decode("utf-8", "replace")), "exists": True,
                "mtime": int(mt) if mt else None}

    def cookies_vault(self) -> dict:
        """The vault's cookies (never their values) and the plain files presets still use."""
        from . import mediadl
        files = sorted({str(Path(p["cookies_file"]).expanduser()) for p in mediadl.load_settings()["presets"]
                        if p["cookies_source"] == "file" and p["cookies_file"]})
        return {"vault": self._vault_cookies_status(),
                "files": [mediadl.cookies_file_status(f) for f in files],
                "imported_from": mediadl.load_settings().get("cookies_imported_from", "")}

    def cookies_import(self, path: str = "") -> dict:
        """A cookies.txt into the vault: the presets that used it (or, with no path, every preset
        that used a file) switch to the vault. The file itself is left as it was."""
        from . import mediadl, vault
        ms = mediadl.load_settings()
        users = [p for p in ms["presets"] if p["cookies_source"] == "file" and p["cookies_file"]]
        if path:
            src = Path(path).expanduser()
        else:
            found = sorted({str(Path(p["cookies_file"]).expanduser()) for p in users})
            if len(found) != 1:
                return {"ok": False, "error": "Elige el archivo cookies.txt a importar."}
            src = Path(found[0])
        status = mediadl.cookies_file_status(str(src))
        if not status["exists"]:
            return {"ok": False, "error": "No se encontró ese archivo de cookies."}
        if not status["youtube_session"]:
            return {"ok": False, "error": "Ese archivo no tiene una sesión de YouTube: expórtalo de nuevo con la sesión iniciada."}
        try:
            data = src.read_bytes()
            if len(data) > 2_000_000:
                return {"ok": False, "error": "Ese archivo es demasiado grande para ser un cookies.txt."}
            vault.put(mediadl.VAULT_COOKIES, data)
        except OSError as exc:
            return {"ok": False, "error": f"No se pudieron guardar las cookies: {exc}"}
        switched = []
        for p in ms["presets"]:
            same_file = p["cookies_source"] == "file" and (not path and p in users
                                                           or Path(p["cookies_file"]).expanduser() == src)
            # Chromium browsers encrypt their cookies for themselves on Windows: yt-dlp can't read
            # them, so those presets had no working cookies; the imported ones serve them now
            locked = p["cookies_source"] == "browser" and p["cookies_browser"] in mediadl.LOCKED_BROWSERS
            if same_file or locked:
                p["cookies_source"], p["cookies_file"] = "vault", ""
                switched.append(p["name"])
        ms["cookies_imported_from"] = str(src)
        mediadl.save_settings(ms)
        log.info("cookies imported into the vault (%d presets)", len(switched))
        return {"ok": True, "presets": switched, "file": str(src), "status": self._vault_cookies_status()}

    def cookies_delete_original(self) -> dict:
        """Deletes the plain cookies.txt that was imported (overwritten first; not to the Recycle
        Bin, where it would stay readable). Only that file, and only once it's in the vault."""
        from . import mediadl, vault
        ms = mediadl.load_settings()
        src = ms.get("cookies_imported_from") or ""
        if not src or not vault.has(mediadl.VAULT_COOKIES):
            return {"ok": False, "error": "No hay un archivo importado para borrar."}
        p = Path(src)
        if any(Path(x["cookies_file"]).expanduser() == p for x in ms["presets"] if x["cookies_source"] == "file"):
            return {"ok": False, "error": "Algún preset todavía usa ese archivo."}
        if p.is_file():
            if p.stat().st_size > 2_000_000:
                return {"ok": False, "error": "Ese archivo ya no parece el cookies.txt importado."}
            vault.wipe(p)
        ms["cookies_imported_from"] = ""
        mediadl.save_settings(ms)
        log.info("imported cookies file deleted")
        return {"ok": True}

    def cookies_forget(self) -> dict:
        """Removes the cookies from the vault; the presets that used them go without cookies."""
        from . import mediadl, vault
        vault.delete(mediadl.VAULT_COOKIES)
        ms = mediadl.load_settings()
        for p in ms["presets"]:
            if p["cookies_source"] == "vault":
                p["cookies_source"], p["cookies_mode"] = "browser", "off"
        mediadl.save_settings(ms)
        return {"ok": True}

    # --------------------------------------------------------------- follows
    # Channels and playlists checked on a schedule: each check just queues
    # the link again; archive.txt skips what is already there, so only what's
    # new is downloaded.

    def _follows_file(self) -> Path:
        return self._qfile.with_name("follows.json")

    def _load_follows(self) -> list[dict]:
        try:
            data = json.loads(self._follows_file().read_text(encoding="utf-8"))
            return [f for f in data.get("follows", []) if isinstance(f, dict) and f.get("url")]
        except (OSError, ValueError):
            return []

    def _save_follows(self, follows: list[dict]) -> None:
        p = self._follows_file()
        tmp = p.with_suffix(".tmp")
        from . import migrate
        tmp.write_text(json.dumps(migrate.stamp("follows.json", {"follows": follows}), ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)

    def follows_list(self) -> dict:
        return {"follows": [f for f in self._load_follows() if f.get("kind") in ("media", "wallhaven")]}

    def follow_add(self, url: str, preset_id: str = "", every_hours: int = 24, only_new: bool = False) -> dict:
        import time as _t
        import uuid as _uuid
        from . import mediadl
        url = str(url or "").strip()
        try:
            every = max(1, min(24 * 30, int(every_hours)))
        except (TypeError, ValueError):
            every = 24
        follows = self._load_follows()
        if any(f["url"].rstrip("/").lower() == url.rstrip("/").lower() for f in follows):
            return {"ok": False, "error": "Ya lo sigues."}
        entry = {"id": _uuid.uuid4().hex[:10], "url": url, "every_hours": every, "added": _t.time(),
                 "last_check": _t.time() if only_new else 0, "enabled": True}
        if _is_web_url(url):
            ms = mediadl.load_settings()
            preset = next((p for p in ms["presets"] if p["id"] == (preset_id or ms["default_preset"])), None)
            if preset is None:
                return {"ok": False, "error": "Ese preset ya no existe."}
            entry.update(kind="media", preset=preset["id"],
                         title=re.sub(r"^https?://(www\.)?", "", url)[:70])
            if only_new:
                try:
                    seen = mediadl.mark_seen(mediadl.destination(preset, _library_root(None)),
                                             mediadl.list_entries(url, preset))
                except mediadl.MediaError as exc:
                    return {"ok": False, "error": str(exc)}
                entry["marked_seen"] = seen
        else:
            return {"ok": False, "error": "Eso no es un enlace que se pueda seguir."}
        follows.append(entry)
        self._save_follows(follows)
        return {"ok": True, "follow": entry}

    def follow_remove(self, follow_id: str) -> dict:
        follows = self._load_follows()
        keep = [f for f in follows if f["id"] != follow_id]
        self._save_follows(keep)
        return {"ok": len(keep) < len(follows)}

    def follow_check_now(self, follow_id: str) -> dict:
        f = next((x for x in self._load_follows() if x["id"] == follow_id), None)
        if f is None:
            return {"ok": False, "error": "Ya no lo sigues."}
        return self._queue_follow(f)

    def _queue_follow(self, f: dict) -> dict:
        import time as _t
        if f.get("kind") == "wallhaven":
            r = self.wallhaven_download(f.get("job") or {})
        else:
            r = self.start("download", f["url"], {"preset": f.get("preset")})
        if r.get("ok") or "ya está en la cola" in str(r.get("error", "")):
            follows = self._load_follows()
            for x in follows:
                if x["id"] == f["id"]:
                    x["last_check"] = _t.time()
            self._save_follows(follows)
        return r

    def _follow_history(self, url: str, entry: dict) -> None:
        """A follow's last 30 checks: new, already there, failed and why (PLAN 0.20.2)."""
        key = str(url or "").rstrip("/").lower()
        follows = self._load_follows()
        hit = False
        for f in follows:
            if f.get("kind") == "media" and f["url"].rstrip("/").lower() == key:
                f["history"] = (f.get("history") or [])[-29:] + [entry]
                hit = True
        if hit:
            self._save_follows(follows)

    def _check_follows(self, now: float) -> int:
        """Queue the follows that are due. Called by the scheduler."""
        n = 0
        for f in self._load_follows():
            if f.get("kind") in ("media", "wallhaven") and f.get("enabled", True) and now >= f.get("last_check", 0) + f.get("every_hours", 24) * 3600:
                if self._queue_follow(f).get("ok"):
                    n += 1
        return n

    def _pause_until_tomorrow(self) -> None:
        import datetime as _dt
        now = _dt.datetime.now()
        tomorrow = (now + _dt.timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        limit, _ = self._budget()
        self.pause_queue(f"Se llegó al límite diario de {limit} descargas de música/video. Sigue sola mañana.",
                         until=tomorrow.timestamp(), scope="media")

    def get_state(self) -> dict:
        return {"version": __version__,
                "settings": load_settings(), "log_path": str(log_file()),
                "running": self.job_running(), "queue": self._queue_state(),
                "last_move": self._last_move(), "moving": self._moving,
                "first_run": self._first_run}

    def first_run_done(self) -> dict:
        """The guided start finished (or was skipped): the settings file marks Bakin as used."""
        self._first_run = False
        save_settings(load_settings())
        return {"ok": True}

    def job_running(self) -> bool:
        return bool(self._worker and self._worker.is_alive())

    # --------------------------------------------------------------- files

    def log_info(self) -> dict:
        """The activity log's size (with its rotated copies), for Configuración › Consumo."""
        base = log_file()
        files = [base, *(base.with_name(f"{base.name}.{i}") for i in range(1, 10))]
        return {"bytes": sum(f.stat().st_size for f in files if f.is_file()), "folder": str(base.parent)}

    def open_logs(self) -> None:
        d = log_file().parent
        if d.is_dir():
            os.startfile(d)  # noqa: S606

    def open_dir(self) -> None:
        if self._job_dir and self._job_dir.is_dir():
            os.startfile(self._job_dir)  # noqa: S606

    # ------------------------------------------------------ general library
    # Bakin's downloads plus the user's own folders (see library.py). The file
    # index is kept in memory and rebuilt when something changes it.

    def _lib_index(self, refresh: bool = False) -> dict:
        from . import library
        root = _library_root(None)
        with self._lib_lock:
            c = self._lib_cache
            if refresh or c is None or c["root"] != str(root):
                cols = library.collections(root)
                c = self._lib_cache = {"root": str(root), "cols": {col.id: col for col in cols},
                                       "items": {col.id: library.scan(col) for col in cols}}
                self._start_colors(c)
            return c

    def _start_colors(self, idx: dict) -> None:
        """Works out, in the background, the dominant colour of the images that
        don't have it yet (the colour filter, PLAN 0.13.3). Gentle: a short rest
        between images; stops when the engine closes."""
        from . import imageinfo, library
        todo = [it for items in idx["items"].values() for it in items
                if it["kind"] == "image" and it.get("width")
                and imageinfo.color(it["path"], it["size"], it["mtime"]) is None]
        if not todo or getattr(self, "_colors_busy", False):
            return
        self._colors_busy = True

        def run():
            import time as _t
            try:
                for i, it in enumerate(todo, 1):
                    if self._closing:
                        break
                    imageinfo.measure_color(it, library.thumb_path(it))
                    if i % 25 == 0:
                        imageinfo.flush()
                    _t.sleep(0.005)
            finally:
                imageinfo.flush()
                self._colors_busy = False

        threading.Thread(target=run, name="bakin-colors", daemon=True).start()

    def _lib_invalidate(self) -> None:
        with self._lib_lock:
            self._lib_cache = None

    def _lib_split(self, ids) -> dict:
        """{collection: [rel, ...]} for ids like "dir:ab12|2026/01/x.jpg"; unknown ids dropped."""
        from . import library
        cols = self._lib_index()["cols"]
        out: dict = {}
        for raw in ids or []:
            cid, _, rel = str(raw).partition("|")
            if cid in cols and library.resolve(cols[cid], rel) is not None:
                out.setdefault(cols[cid], []).append(rel)
        return out

    def library_roots(self) -> list[str]:
        """Folders whose files the window may show (videos, audio, full images)."""
        idx = self._lib_index()
        return [idx["root"]] + [str(c.root) for c in idx["cols"].values() if c.kind == "folder"]

    def wallpaper_sources(self) -> dict:
        """Where a monitor's rotating backgrounds can come from (PLAN 0.13.2):
        each collection's folder, and the favourites of all of them (the shell
        reads their meta files itself, so it rotates with the engine asleep)."""
        from . import library
        cols = library.collections(_library_root(None))
        out = [{"kind": "favorites", "label": "Favoritas", "folders": [],
                "metas": [{"root": str(c.root), "meta": str(c.meta_path)} for c in cols]}]
        out += [{"kind": "folder", "label": c.name, "folders": [str(c.root)], "metas": []} for c in cols]
        return {"sources": out}

    def library_overview(self, refresh: bool = False) -> dict:
        from . import library, mediadl
        if library.sync_preset_folders(_library_root(None), mediadl.load_settings()["presets"]):
            refresh = True  # a preset's folder just joined the library
        idx = self._lib_index(refresh)
        cols, tags = [], {}
        kinds = dict.fromkeys(library.KINDS, 0)
        total = favs = deleted = 0
        # Fotos and Música are separate spaces in the window: photo-side counts
        # leave the songs out, and each collection says which space it belongs to
        photos = photo_favs = deleted_audio = 0
        for cid, col in idx["cols"].items():
            items = idx["items"][cid]
            audio = 0
            for it in items:
                kinds[it["kind"]] += 1
                favs += it["fav"]
                if it["kind"] == "audio":
                    audio += 1
                else:
                    photos += 1
                    photo_favs += it["fav"]
                    for t in it["tags"]:
                        tags[t] = tags.get(t, 0) + 1
            gone = library.Meta(col).deleted
            n_deleted = len(gone)
            deleted += n_deleted
            deleted_audio += sum(1 for rel in gone if library.kind_of(Path(rel).suffix[1:]) == "audio")
            total += len(items)
            cover = next((library.thumb(it, 160) for it in sorted(items, key=lambda i: -i["date"])[:5]
                          if it["kind"] in ("image", "audio")), "")
            cols.append({"id": cid, "kind": col.kind, "name": col.name, "path": str(col.root),
                         "count": len(items), "bytes": sum(i["size"] for i in items),
                         "exists": col.root.is_dir(), "cover": cover,
                         "audio": audio, "music": audio * 2 > len(items)})
        cols.sort(key=lambda c: c["name"].lower())
        return {"root": idx["root"], "collections": cols, "total": total, "favorites": favs,
                "deleted": deleted, "kinds": kinds,
                "photos": photos, "photo_favorites": photo_favs,
                "deleted_photos": deleted - deleted_audio, "deleted_music": deleted_audio,
                "tags": sorted(({"name": k, "count": v} for k, v in tags.items()),
                               key=lambda t: (-t["count"], t["name"].lower()))}

    # ----------------------------------------------------------------- music
    # The player runs in the window; the engine hands it the songs of a list,
    # their covers, and keeps play counts and the player's state.

    def music_tracks(self, filters: dict | None = None, ids: list | None = None) -> dict:
        """The songs of a library list (any filter, audio only), or the given
        ids in their order: what the player needs, without covers."""
        from . import library, music
        if ids:
            idx = self._lib_index()
            by_id = {it["id"]: it for items in idx["items"].values() for it in items if it["kind"] == "audio"}
            found = [by_id[i] for i in (str(x) for x in ids) if i in by_id]
        else:
            found = self._query({**(filters or {}), "kinds": ["audio"]})
        plays = music.plays()
        gains = self._gains(found[:music.MAX_QUEUE])
        tracks = []
        for it in found[:music.MAX_QUEUE]:
            p = plays.get(it["id"]) or {}
            tracks.append({"id": it["id"], "path": it["path"], "name": it["name"], "gain_db": gains.get(it["id"]),
                           "title": it.get("title") or Path(it["name"]).stem, "artist": it.get("artist") or "",
                           "album": it.get("album") or "", "duration": it.get("duration"),
                           "codec": it.get("codec") or it.get("ext"), "fav": it.get("fav", False),
                           "plays": p.get("n", 0), "last_played": p.get("last", 0)})
        return {"tracks": tracks}

    def _query(self, filters: dict | None) -> list[dict]:
        """The items of a library list, in order. A "music" filter picks one
        of Bakin Música's views (most played, an artist, a playlist, a mix…)
        over the songs that pass the other filters."""
        from . import library, music
        idx = self._lib_index()
        everything = [it for items in idx["items"].values() for it in items]
        f = dict(filters or {})
        view = f.pop("music", None)
        if not isinstance(view, dict) or not view.get("view"):
            return library.query(everything, f)
        plays = music.plays()
        songs = []
        for it in everything:
            if it["kind"] == "audio":
                p = plays.get(it["id"]) or {}
                songs.append({**it, "plays": p.get("n", 0), "last_played": p.get("last", 0)})
        v, value = view["view"], str(view.get("value") or "")
        ordered = v in ("top", "recent", "playlist", "mix", "rules", "added", "queue")
        base = library.query(songs, {**f, "kinds": ["audio"], "sort": "new" if ordered else f.get("sort")})
        if v == "top":
            return music.sort_items([i for i in base if i["plays"]], "plays")
        if v == "recent":
            return music.sort_items([i for i in base if i["last_played"]], "last_played")
        if v == "never":
            return [i for i in base if not i["plays"]]
        if v == "favorites":
            return [i for i in base if i.get("fav")]
        if v == "artist":
            return [i for i in base if (i.get("artist") or "").casefold() == value.casefold()]
        if v == "album":
            artist = str(view.get("artist") or "").casefold()
            return [i for i in base if (i.get("album") or "").casefold() == value.casefold()
                    and (not artist or (i.get("artist") or "").casefold() == artist)]
        if v == "playlist":
            pl = music.playlist(value)
            if pl is None:
                return []
            if pl["kind"] == "smart":
                return music.apply_rules(base, pl.get("rules"))
            by_id = {i["id"]: i for i in base}
            return [by_id[i] for i in pl["ids"] if i in by_id]
        if v == "mix":
            mix = next((m for m in music.mixes(songs) if m["id"] == value), None)
            by_id = {i["id"]: i for i in base}
            return [by_id[i] for i in (mix["ids"] if mix else []) if i in by_id]
        if v == "queue":  # the player's queue from the playing song on, in its order
            by_id = {i["id"]: i for i in base}
            return [by_id[i] for i in (str(x) for x in (view.get("ids") or [])) if i in by_id]
        if v == "rules":  # a smart playlist being edited: how many songs match
            return music.apply_rules(base, view.get("rules"))
        if v == "added":
            return music.sort_items(base, "new")
        if v == "radio":  # «Radio desde este tema» (PLAN 0.14.4)
            by_id = {i["id"]: i for i in base}
            return [by_id[i["id"]] for i in music.radio(songs, value) if i["id"] in by_id]
        return base  # "songs"

    def music_overview(self) -> dict:
        """What the Música card in the sidebar shows."""
        from . import music
        songs = self._query({"music": {"view": "songs"}})
        artists = {(i.get("artist") or "").casefold() for i in songs if i.get("artist")}
        albums = {((i.get("album") or "").casefold(), (i.get("artist") or "").casefold()) for i in songs if i.get("album")}
        lists = []
        for p in music.playlists():
            n = len(self._query({"music": {"view": "playlist", "value": p["id"]}}))
            lists.append({"id": p["id"], "name": p["name"], "kind": p["kind"], "count": n,
                          "rules": p.get("rules"), "mirror": (p.get("mirror") or {}).get("folder", "")})
        return {"songs": len(songs), "artists": len(artists), "albums": len(albums),
                "favorites": sum(1 for i in songs if i.get("fav")),
                "played": sum(1 for i in songs if i["plays"]), "never": sum(1 for i in songs if not i["plays"]),
                "playlists": lists,
                "mixes": [{"id": m["id"], "name": m["name"], "desc": m["desc"], "count": len(m["ids"])}
                          for m in music.mixes(songs)]}

    def music_groups(self, by: str = "artist", filters: dict | None = None) -> dict:
        """Artists or albums of the songs in a list, with a song for the cover."""
        songs = self._query({**(filters or {}), "music": {"view": "songs"}})
        groups: dict[tuple, dict] = {}
        for i in songs:
            name = (i.get("album") if by == "album" else i.get("artist")) or ""
            artist = (i.get("artist") or "") if by == "album" and name else ""
            key = (name.casefold(), artist.casefold())
            g = groups.get(key)
            if g is None:
                g = groups[key] = {"name": name, "artist": artist, "count": 0, "duration": 0, "plays": 0,
                                   "cover_id": i["id"]}
            g["count"] += 1
            g["duration"] += i.get("duration") or 0
            g["plays"] += i["plays"]
        out = sorted(groups.values(), key=lambda g: (not g["name"], g["name"].casefold(), g["artist"].casefold()))
        return {"groups": out}

    def music_playlist_save(self, name: str, kind: str = "list", playlist_id: str = "", rules: dict | None = None,
                            ids: list | None = None) -> dict:
        from . import music
        try:
            pl = music.playlist_save(playlist_id or None, name, kind, rules, ids)
            self._mirror_soon(pl["id"])
            return {"ok": True, "playlist": pl}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def music_playlist_add(self, playlist_id: str, ids: list) -> dict:
        from . import music
        try:
            res = {"ok": True, "added": music.playlist_add(playlist_id, ids)}
            self._mirror_soon(playlist_id)
            return res
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def music_playlist_remove(self, playlist_id: str, ids: list) -> dict:
        from . import music
        res = {"ok": True, "removed": music.playlist_remove(playlist_id, ids)}
        self._mirror_soon(playlist_id)
        return res

    def music_playlist_delete(self, playlist_id: str) -> dict:
        from . import music
        return {"ok": music.playlist_delete(playlist_id)}

    def music_playlist_export(self, playlist_id: str, path: str) -> dict:
        """Writes the playlist as .m3u8 where the user chose (a save dialog)."""
        from . import music
        pl = music.playlist(playlist_id)
        if pl is None:
            return {"ok": False, "error": "Esa playlist ya no existe."}
        dest = Path(path)
        if dest.suffix.lower() not in (".m3u8", ".m3u"):
            dest = dest.with_suffix(".m3u8")
        items = self._query({"music": {"view": "playlist", "value": playlist_id}})
        try:
            dest.write_text(music.m3u8(items), encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "error": f"No se pudo guardar: {exc.strerror or exc}"}
        return {"ok": True, "path": str(dest), "count": len(items)}

    # ---------------------------------------------- mirror folders (0.20.1)

    MIRROR_DELAY = 30.0  # seconds after the last change to a playlist before its folder follows

    @staticmethod
    def _mirror_forbidden() -> list[Path]:
        from . import library
        root = _library_root(None)
        return [root] + [c.root for c in library.collections(root)]

    def music_mirror_set(self, playlist_id: str, folder: str, mode: str = "copy", adopt: bool = False) -> dict:
        """«Carpeta espejo…»: keep `folder` equal to the playlist (for the car, the phone, another
        player). A folder with things in it is only taken when the user said so (`adopt`)."""
        from . import mirror, music
        pl = music.playlist(playlist_id)
        if pl is None:
            return {"ok": False, "error": "Esa playlist ya no existe."}
        target = Path(str(folder or "")).expanduser()
        if not target.is_absolute():
            return {"ok": False, "error": "Usa una ruta completa."}
        songs = self._query({"music": {"view": "playlist", "value": playlist_id}})
        try:
            p = mirror.plan(songs, target, mode, self._mirror_forbidden(), adopt=adopt)
        except mirror.MirrorError as exc:
            if not adopt and target.is_dir() and mirror.read_manifest(target) is None and any(target.iterdir()):
                return {"ok": False, "needs_adopt": True, "error": str(exc)}
            return {"ok": False, "error": str(exc)}
        music.playlist_mirror(playlist_id, {"folder": str(target), "mode": mode, "remove_ok": False,
                                            "adopt": bool(adopt)})
        self._mirror_soon(playlist_id, 0.0)
        return {"ok": True, "copy": len(p["copy"]), "folder": str(target)}

    @staticmethod
    def music_mirror_off(playlist_id: str) -> dict:
        """Stops mirroring; what's in the folder stays."""
        from . import music
        return {"ok": music.playlist_mirror(playlist_id, None)}

    def music_mirror_sync(self, playlist_id: str, allow_remove: bool = False) -> dict:
        """«Sincronizar ahora», and the user's yes to removing (to the Recycle Bin) the first time."""
        from . import music
        pl = music.playlist(playlist_id)
        if pl is None or not pl.get("mirror"):
            return {"ok": False, "error": "Esa playlist no tiene carpeta espejo."}
        if allow_remove and not pl["mirror"].get("remove_ok"):
            music.playlist_mirror(playlist_id, {**pl["mirror"], "remove_ok": True})
        self._mirror_soon(playlist_id, 0.0)
        return {"ok": True}

    def _mirror_soon(self, playlist_id: str, delay: float | None = None) -> None:
        """A mirrored playlist changed: its folder follows a little later (one sync for many edits)."""
        from . import music
        pl = music.playlist(playlist_id)
        if pl is None or not pl.get("mirror"):
            return
        with self._mirror_lock:
            old = self._mirror_timers.pop(playlist_id, None)
            if old:
                old.cancel()
            t = threading.Timer(self.MIRROR_DELAY if delay is None else delay, self._mirror_run, (playlist_id,))
            t.daemon = True
            self._mirror_timers[playlist_id] = t
            t.start()

    def mirror_all_soon(self) -> None:
        """At start: every mirrored playlist catches up (smart ones change with the library)."""
        from . import music
        for pl in music.playlists():
            if pl.get("mirror"):
                self._mirror_soon(pl["id"])

    def _mirror_run(self, playlist_id: str) -> dict | None:
        with self._mirror_lock:
            self._mirror_timers.pop(playlist_id, None)
        with self._mirror_busy:
            return self._mirror_sync(playlist_id)

    def _mirror_sync(self, playlist_id: str) -> dict | None:
        from . import library, mirror, music
        pl = music.playlist(playlist_id)
        conf = (pl or {}).get("mirror")
        if not conf or self._closing:
            return None
        folder = Path(conf["folder"])
        base = {"kind": "mirror_done", "playlist": playlist_id, "name": pl["name"], "folder": str(folder)}
        if not folder.is_dir():  # a phone or a USB drive that isn't plugged in now: next time
            self._push({**base, "ok": False, "error": "La carpeta espejo no está conectada ahora."})
            return None
        songs = self._query({"music": {"view": "playlist", "value": playlist_id}})
        try:
            p = mirror.plan(songs, folder, conf.get("mode", "copy"), self._mirror_forbidden(),
                            adopt=bool(conf.get("adopt")))
        except mirror.MirrorError as exc:
            self._push({**base, "ok": False, "error": str(exc)})
            return None
        if mirror.is_empty(p):
            return {"copied": 0, "updated": 0, "removed": 0, "failed": [], "ask_remove": 0}
        ask = 0
        if p["remove"] and not conf.get("remove_ok"):
            ask, p["remove"] = len(p["remove"]), []  # removing waits for the user's first yes

        def trash(paths):
            for path in paths:
                library.send_to_recycle_bin(path)

        def progress(step, total, name):
            self._push({"kind": "mirror_progress", "playlist": playlist_id, "step": step, "total": total})

        res = mirror.apply(p, folder, trash, progress, lambda: self._closing)
        failed = [{"file": f["file"], "error": redact.redact(str(f["error"]))[:200]} for f in res["failed"]]
        if failed:
            log.warning("mirror sync: %d file(s) failed", len(failed))
        self._push({**base, "ok": True, "copied": res["copied"], "updated": res["updated"],
                    "removed": res["removed"], "failed": failed, "ask_remove": ask})
        return {**res, "failed": failed, "ask_remove": ask}

    # ------------------------------------- the home server's data (0.21.1)
    # The shell's OpenSubsonic server (lan.rs) asks for these; nothing here listens on the network.

    LAN_CODE = "lan-code"
    _CODE_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L: read off a screen and typed on a phone

    def lan_code(self, new: bool = False) -> dict:
        """The pairing code: the Subsonic app's password (user «bakin»). Random, kept encrypted in
        the vault, never one of the user's passwords; `new` makes another (every phone has to pair
        again: that's how a device is removed)."""
        import secrets
        from . import vault
        raw = None if new else vault.get(self.LAN_CODE)
        code = raw.decode("ascii", "replace") if raw else ""
        if len(code) != 9:
            code = "".join(secrets.choice(self._CODE_CHARS) for _ in range(8))
            code = f"{code[:4]}-{code[4:]}"
            vault.put(self.LAN_CODE, code.encode("ascii"))
        return {"code": code}

    def lan_library(self) -> dict:
        """Every song and playlist, for the phone's Subsonic app."""
        from . import music
        songs = self._query({"music": {"view": "songs"}, "kinds": ["audio"]})
        plays = music.plays()
        out = []
        for it in songs:
            p = plays.get(it["id"]) or {}
            year = str(it.get("year") or "")[:4]
            out.append({"id": it["id"], "path": it["path"], "title": it.get("title") or Path(it["name"]).stem,
                        "artist": it.get("artist") or "", "album": it.get("album") or "",
                        "duration": int(it.get("duration") or 0), "track": None,
                        "year": int(year) if year.isdigit() else None, "size": int(it.get("size") or 0),
                        "suffix": Path(it["name"]).suffix[1:].lower(), "bitrate": it.get("bitrate"),
                        "starred": bool(it.get("fav")), "plays": int(p.get("n", 0)), "added": float(it.get("date") or 0)})
        lists = []
        for pl in music.playlists():
            ids = [i["id"] for i in self._query({"music": {"view": "playlist", "value": pl["id"]}})]
            lists.append({"id": pl["id"], "name": pl["name"], "song_ids": ids})
        return {"songs": out, "playlists": lists}

    def lan_star(self, id: str, on: bool) -> dict:
        if str(id) not in self._songs_by_id([id]):
            return {"ok": False}
        return self.library_set_favorite([str(id)], bool(on))

    def lan_scrobble(self, id: str) -> dict:
        if str(id) not in self._songs_by_id([id]):
            return {"ok": False}
        return self.music_played(str(id))

    def lan_cover(self, id: str, size: int = 320) -> dict:
        """A song's cover as a file of the preview cache (the shell reads it), or ""."""
        from . import library
        it = self._songs_by_id([id]).get(str(id))
        return {"path": library.thumb_file(it, max(64, min(int(size), 640))) if it else ""}

    def lan_lyrics(self, id: str) -> dict:
        from . import lyrics
        it = self._songs_by_id([id]).get(str(id))
        return {"text": (lyrics.find(Path(it["path"])) or "") if it else ""}

    def music_cover(self, item_id: str, size: int = 320) -> dict:
        from . import library
        idx = self._lib_index()
        it = next((x for items in idx["items"].values() for x in items if x["id"] == item_id), None)
        if it is None or it["kind"] != "audio":
            return {"cover": ""}
        return {"cover": library.thumb(it, max(64, min(int(size), 640)))}

    # ------------------------------------------------ a clean library (0.14)

    def _songs_by_id(self, ids) -> dict:
        idx = self._lib_index()
        want = {str(i) for i in ids} if ids else None
        return {it["id"]: it for items in idx["items"].values() for it in items
                if it["kind"] == "audio" and (want is None or it["id"] in want)}

    def music_clean_propose(self, ids: list | None = None) -> dict:
        """«Limpiar títulos…» (PLAN 0.14.1): what tagclean would change, song by song; nothing is
        written. Sure ones first."""
        from . import tagclean, tagfix
        out = []
        for it in self._songs_by_id(ids).values():
            p = tagclean.propose({"name": it["name"], "title": it.get("title") or "",
                                  "artist": it.get("artist") or "", "album": it.get("album") or ""})
            if p:
                out.append({"id": it["id"], "name": it["name"],
                            "before": {"artist": it.get("artist") or "", "title": it.get("title") or "",
                                       "album": it.get("album") or ""},
                            "after": {"artist": p["artist"], "title": p["title"], "album": p["album"]},
                            "featuring": p["featuring"], "confidence": p["confidence"], "why": p["why"]})
        out.sort(key=lambda r: (r["confidence"] != "high", r["after"]["artist"].casefold(), r["after"]["title"].casefold()))
        return {"items": out, "undo": tagfix.last_batch()}

    def music_clean_apply(self, changes: list, playing: str = "") -> dict:
        """Writes the changes the user ticked ([{id, title?, artist?, album?, year?, release?}]);
        `release` embeds that MusicBrainz release's cover (looked up already). The song playing is
        skipped: Windows keeps it open."""
        from . import tagfix
        songs = self._songs_by_id([c.get("id") for c in changes or []])
        todo = []
        for c in changes or []:
            it = songs.get(str(c.get("id")))
            if not it:
                continue
            ch = {"id": it["id"], "path": it["path"]}
            for k in ("title", "artist", "album", "year"):
                if c.get(k) is not None:
                    ch[k] = " ".join(str(c[k]).split())[:300]
            if c.get("release") and self._mb_covers.get(str(c["release"])):
                ch["cover"] = self._mb_covers[str(c["release"])]
            todo.append(ch)
        res = tagfix.apply(todo, skip_paths={songs[playing]["path"]} if playing in songs else set())
        self._lib_invalidate()
        n = len(res["done"])
        msg = f"{n} tema{'s' if n != 1 else ''} corregido{'s' if n != 1 else ''}."
        if res["skipped"]:
            msg += " El que está sonando queda para después."
        if res["failed"]:
            msg += f" {len(res['failed'])} no: {res['failed'][0]['error']}"
        return {"ok": not res["failed"], "message": msg, **res}

    def music_tags_undo(self) -> dict:
        from . import tagfix
        res = tagfix.undo()
        self._lib_invalidate()
        n = len(res["done"])
        return {"ok": bool(n) and not res["failed"], "message": f"{n} tema{'s' if n != 1 else ''} como estaba{'n' if n != 1 else ''}.",
                **res}

    # Configuración › Rendimiento (PLAN 0.15.5): only these keys, each checked
    SETTABLE = {"catalog_hourly": lambda v: max(0, min(1000, int(v))), "catalog_covers": bool,
                "loudness_mode": lambda v: v if v in ("off", "track", "album") else "off",
                "loudness_preamp": lambda v: max(0.0, min(6.0, float(v))),
                "lyrics_online": bool, "lyrics_beside": bool, "lan_enabled": bool,
                "lan_port": lambda v: int(v) if 1024 <= int(v) <= 65535 else 4533,
                "lang": lambda v: v if v in ("auto", "es", "en") else "es"}

    def settings_set(self, changes: dict) -> dict:
        s = load_settings()
        for k, v in (changes or {}).items():
            if k in self.SETTABLE:
                try:
                    s[k] = self.SETTABLE[k](v)
                except (TypeError, ValueError):
                    return {"ok": False, "error": f"Valor inválido para {k}."}
        save_settings(s)
        return {"ok": True, "settings": {k: s[k] for k in self.SETTABLE}}

    def music_mb_enable(self, on: bool) -> dict:
        """Configuración › Música › «Consultar MusicBrainz» (off by default)."""
        save_settings({**load_settings(), "musicbrainz": bool(on)})
        return {"ok": True, "on": bool(on)}

    def music_mb_start(self, ids: list) -> dict:
        """MusicBrainz for some songs (PLAN 0.14.2), in the background: `mb_result` per song,
        `mb_done` at the end. Only with the setting on; only title, artist and length are sent."""
        if not load_settings().get("musicbrainz"):
            return {"ok": False, "error": "Activa «Consultar MusicBrainz» en Configuración › Música."}
        if self._mb_busy:
            return {"ok": False, "error": "Ya se está consultando MusicBrainz."}
        songs = list(self._songs_by_id(ids).values())[:300]
        if not songs:
            return {"ok": False, "error": "Elige algunos temas."}
        self._mb_cancel.clear()
        self._mb_busy = True
        threading.Thread(target=self._mb_run, args=(songs,), name="bakin-musicbrainz", daemon=True).start()
        return {"ok": True, "count": len(songs)}

    def music_mb_cancel(self) -> dict:
        self._mb_cancel.set()
        return {"ok": True}

    def _mb_run(self, songs: list[dict]) -> None:
        from . import musicbrainz, tagclean
        found = 0
        note = ""
        try:
            for n, it in enumerate(songs, 1):
                if self._mb_cancel.is_set():
                    note = "Cancelado."
                    break
                # the cleaned-up title and artist find more than YouTube's raw ones
                p = tagclean.propose({"name": it["name"], "title": it.get("title") or "",
                                      "artist": it.get("artist") or "", "album": it.get("album") or ""})
                title = (p or {}).get("title") or it.get("title") or Path(it["name"]).stem
                artist = (p or {}).get("artist") or it.get("artist") or ""
                try:
                    hit = musicbrainz.lookup(title, artist, it.get("duration"))
                    cover = False
                    if hit and hit.get("release_mbid") and hit["release_mbid"] not in self._mb_covers:
                        img = musicbrainz.cover(hit["release_mbid"])
                        if img:
                            self._mb_covers[hit["release_mbid"]] = img
                    cover = bool(hit and self._mb_covers.get(hit.get("release_mbid") or ""))
                except musicbrainz.Busy:
                    note = "MusicBrainz pidió esperar: probá de nuevo en un minuto."
                    break
                except musicbrainz.MBError as exc:
                    self._push({"kind": "mb_result", "id": it["id"], "index": n, "count": len(songs), "error": str(exc)})
                    continue
                if hit:
                    found += 1
                self._push({"kind": "mb_result", "id": it["id"], "index": n, "count": len(songs),
                            "before": {"artist": it.get("artist") or "", "title": it.get("title") or "",
                                       "album": it.get("album") or ""},
                            "after": {k: hit[k] for k in ("artist", "title", "album", "year")} if hit else None,
                            "release": hit.get("release_mbid") if hit else None, "cover": cover})
        finally:
            self._mb_busy = False
            self._push({"kind": "mb_done", "found": found, "count": len(songs), "note": note})

    def music_dupes(self) -> dict:
        """«Duplicados» (PLAN 0.14.3): groups of the same song, the best copy first."""
        from . import dupes, music
        songs = self._songs_by_id(None)
        plays = music.plays()
        out = []
        for g in dupes.groups(list(songs.values())):
            items = []
            for i in g["items"]:
                it = songs[i]
                items.append({k: it.get(k) for k in ("id", "name", "title", "artist", "album", "duration", "codec",
                                                       "bitrate", "lossless", "size", "fav", "path")}
                             | {"plays": (plays.get(i) or {}).get("n", 0)})
            out.append({**g, "items": items})
        return {"groups": out}

    def music_dupes_resolve(self, keep: str, drop: list) -> dict:
        """Keeps one copy: the others' plays, favourite and playlist places go to it, then they go to
        the Recycle Bin («Deshacer» restores them, as any deletion)."""
        from . import library, music
        songs = self._songs_by_id([keep, *drop])
        if keep not in songs:
            return {"ok": False, "error": "Ese tema ya no está."}
        drop = [d for d in (str(x) for x in drop) if d in songs and d != keep]
        fav = any(songs[d].get("fav") for d in drop)
        for d in drop:
            music.move_plays(d, keep)
            music.rename_ids(d, keep)
        if fav and not songs[keep].get("fav"):
            self.library_set_favorite([keep], True)
        res = self.library_delete(drop)
        return {**res, "kept": keep}

    def music_stats(self, span: str = "month") -> dict:
        """Música › «Estadísticas» (PLAN 0.14.5), from the play log: plays, minutes, top songs and
        artists, artists new in the span. Spans: week, month, year, all."""
        from . import music
        now = time.time()
        days = {"week": 7, "month": 30, "year": 365}.get(span)
        since = now - days * 86400 if days else 0
        log = music.play_log()
        songs = self._songs_by_id(None)
        in_span = [(i, at) for i, at in log if at >= since]
        first_seen: dict[str, float] = {}
        for i, at in log:
            a = music.main_artist((songs.get(i) or {}).get("artist") or "")
            if a and a not in first_seen:
                first_seen[a] = at
        count: dict[str, int] = {}
        minutes = 0.0
        for i, _ in in_span:
            count[i] = count.get(i, 0) + 1
            minutes += float((songs.get(i) or {}).get("duration") or 0) / 60
        by_artist: dict[str, dict] = {}
        for i, n in count.items():
            it = songs.get(i)
            if not it or not it.get("artist"):
                continue
            key = music.main_artist(it["artist"])
            e = by_artist.setdefault(key, {"artist": it["artist"].split(",")[0].strip(), "plays": 0, "id": i})
            e["plays"] += n
        top = sorted(((n, i) for i, n in count.items() if i in songs), key=lambda x: -x[0])[:10]
        hours = [0] * 24
        for _, at in in_span:
            hours[time.localtime(at).tm_hour] += 1
        return {"span": span, "plays": len(in_span), "minutes": round(minutes),
                "songs": len(count), "since_log": log[0][1] if log else None,
                "top_songs": [{"id": i, "plays": n, "title": songs[i].get("title") or Path(songs[i]["name"]).stem,
                               "artist": songs[i].get("artist") or ""} for n, i in top],
                "top_artists": sorted(by_artist.values(), key=lambda e: -e["plays"])[:10],
                "new_artists": sorted({by_artist[a]["artist"] for a, t in first_seen.items() if t >= since and a in by_artist}),
                "hours": hours}

    # ---------------------------------------- YouTube Music's catalogue (0.15)

    def _catalog_call(self, fn) -> dict:
        from . import ytmusic
        try:
            return {"ok": True, **fn(int(load_settings().get("catalog_hourly") or 0))}
        except ytmusic.Busy as exc:
            return {"ok": False, "error": str(exc), "until": exc.until}
        except ytmusic.CatalogError as exc:
            log.warning("catalogue: %s (%s)", exc, exc.__cause__.__class__.__name__ if exc.__cause__ else "")
            return {"ok": False, "error": str(exc)}

    def _have_ids(self) -> set[str]:
        """YouTube ids already in the music folder (its archive), to mark the catalogue's cards."""
        from . import mediadl
        ms = mediadl.load_settings()
        preset = next((p for p in ms["presets"] if p["id"] == ms["default_preset"]), ms["presets"][0])
        try:
            return mediadl.archived_ids(mediadl.destination(preset, _library_root(None)))
        except OSError:
            return set()

    def _mark_have(self, cards: list[dict]) -> list[dict]:
        have = self._have_ids()
        for c in cards:
            if c.get("kind") == "song":
                c["have"] = c["id"] in have
        return cards

    def catalog_search(self, q: str, kind: str = "songs") -> dict:
        """Música en «Descargar» (PLAN 0.15.1): YouTube Music's catalogue, without an account."""
        from . import ytmusic
        return self._catalog_call(lambda hourly: {"items": self._mark_have(ytmusic.search(q, kind, hourly))})

    def catalog_page(self, kind: str, id: str) -> dict:
        """An artist's page (songs, albums) or an album's / a playlist's songs."""
        from . import ytmusic
        def page(hourly):
            if kind == "artist":
                a = ytmusic.artist(str(id), hourly)
                self._mark_have(a["songs"])
                return {"page": a}
            p = (ytmusic.album if kind == "album" else ytmusic.playlist)(str(id), hourly)
            self._mark_have(p["tracks"])
            return {"page": p}
        return self._catalog_call(page)

    def catalog_thumbs(self, urls: list) -> dict:
        from . import ytmusic
        return {"thumbs": ytmusic.thumbs([str(u) for u in (urls or [])][:80])}

    def catalog_download(self, items: list, preset: str = "") -> dict:
        """The cards ticked, to the queue: each song alone, an album or playlist as its list."""
        from . import ytmusic
        queued, failed = 0, []
        for it in items or []:
            try:
                url = ytmusic.link(it)
            except ytmusic.CatalogError as exc:
                failed.append(str(exc))
                continue
            if not url:
                continue
            res = self.start("download", url, {"preset": preset} if preset else {})
            if res.get("ok"):
                queued += 1
            else:
                failed.append(res.get("error") or "")
        msg = f"{queued} en cola." if queued else "No se pudo poner nada en la cola."
        if failed:
            msg += f" {len(failed)} no: {failed[0]}"
        return {"ok": bool(queued), "queued": queued, "message": msg}

    # ---------------------------------------------- Spotify → YouTube (0.15.2)

    def spotify_resolve(self, url: str = "", csv_text: str = "") -> dict:
        """A Spotify link (or an exported CSV): its songs, each looked up on YouTube Music in the
        background (`sp_track` per song, `sp_done` at the end). Only names and lengths come from
        Spotify; the downloads are YouTube's."""
        from . import spotify
        if self._sp_busy:
            return {"ok": False, "error": "Ya se están buscando los temas de otra lista."}
        try:
            data = spotify.from_csv(csv_text) if csv_text else spotify.fetch(url)
        except spotify.SpotifyError as exc:
            return {"ok": False, "error": str(exc)}
        if not data["tracks"]:
            return {"ok": False, "error": "Esa lista no tiene temas."}
        self._sp_cancel.clear()
        self._sp_busy = True
        threading.Thread(target=self._sp_run, args=(data["tracks"],), name="bakin-spotify", daemon=True).start()
        return {"ok": True, "name": data["name"], "kind": data["kind"], "count": len(data["tracks"]),
                "limited": data.get("limited", False), "tracks": data["tracks"]}

    @staticmethod
    def read_text_file(path: str) -> dict:
        """A .csv the user picked (Spotify's export), as text: only .csv, at most 5 MB."""
        p = Path(str(path or ""))
        if p.suffix.lower() != ".csv" or not p.is_file():
            return {"ok": False, "error": "Elige un archivo .csv."}
        if p.stat().st_size > 5_000_000:
            return {"ok": False, "error": "Ese archivo es demasiado grande."}
        try:
            return {"ok": True, "text": p.read_text(encoding="utf-8-sig", errors="replace")}
        except OSError as exc:
            return {"ok": False, "error": f"No se pudo leer: {exc}"}

    def spotify_cancel(self) -> dict:
        self._sp_cancel.set()
        return {"ok": True}

    def _sp_run(self, tracks: list[dict]) -> None:
        from . import matching, ytmusic
        have = self._have_ids()
        note, found = "", 0
        hourly = int(load_settings().get("catalog_hourly") or 0)
        try:
            for n, t in enumerate(tracks):
                if self._sp_cancel.is_set():
                    note = "Cancelado."
                    break
                try:
                    cards = ytmusic.search(matching.query(t), "songs", hourly * 3 if hourly else 0)
                except ytmusic.Busy as exc:
                    note = f"{exc} Lo encontrado hasta acá se puede bajar igual."
                    break
                except ytmusic.CatalogError as exc:
                    self._push({"kind": "sp_track", "index": n, "error": str(exc)})
                    continue
                for c in cards:
                    c["have"] = c["id"] in have
                b = matching.best(t, cards)
                found += b["verdict"] != "no"
                self._push({"kind": "sp_track", "index": n, "count": len(tracks), **b})
                self._sp_cancel.wait(0.5)  # gentle with YouTube
        finally:
            self._sp_busy = False
            self._push({"kind": "sp_done", "found": found, "count": len(tracks), "note": note})

    # ------------------------------------------------------ sound (0.16)

    def _gains(self, songs: list[dict]) -> dict:
        """{id: dB} for even volume in the mode chosen (empty when it's off)."""
        from . import loudness
        s = load_settings()
        if s.get("loudness_mode", "off") == "off":
            return {}
        return loudness.gains_for(songs, s["loudness_mode"], float(s.get("loudness_preamp") or 0))

    def music_gains(self, ids: list | None = None) -> dict:
        """The gains of some songs (the player's), after the mode changed or a scan measured more."""
        return {"gains": self._gains(list(self._songs_by_id(ids).values()))}

    def music_loudness_scan(self, stop: bool = False) -> dict:
        """Measures the songs not measured yet (ffmpeg's EBU R128, one at a time, low priority),
        in the background: `loud_progress` events, `loud_done` at the end."""
        from . import loudness
        if stop:
            self._loud_cancel.set()
            return {"ok": True}
        if self._loud_busy:
            return {"ok": True, "running": True}
        songs = [it for it in self._songs_by_id(None).values()]
        self._loud_cancel.clear()
        self._loud_busy = True

        def run():
            try:
                res = loudness.scan(songs, self._loud_cancel,
                                    lambda done, total: self._push({"kind": "loud_progress", "done": done, "total": total}),
                                    prune=True)
            except Exception as exc:  # never take the engine down for a measure
                log.warning("loudness scan failed: %s", exc.__class__.__name__)
                res = {"measured": 0, "skipped": 0, "failed": 0}
            finally:
                self._loud_busy = False
            self._push({"kind": "loud_done", **res})
        threading.Thread(target=run, name="bakin-loudness", daemon=True).start()
        return {"ok": True, "count": len(songs)}

    def music_lyrics(self, id: str) -> dict:
        """A song's lyrics if there are some here (its .lrc next to it, or Bakin's copy)."""
        from . import lyrics
        it = self._songs_by_id([id]).get(str(id))
        if not it:
            return {"ok": False, "error": "Ese tema ya no está."}
        text = lyrics.find(Path(it["path"]))
        if text is None:
            return {"ok": True, "found": False, "online": bool(load_settings().get("lyrics_online"))}
        return {"ok": True, "found": True, **lyrics.parse_lrc(text)}

    def music_lyrics_fetch(self, id: str) -> dict:
        """LRCLIB for a song (only with the setting on: sends title, artist, album and length);
        what it finds is kept as a .lrc."""
        from . import lyrics, tagclean
        s = load_settings()
        if not s.get("lyrics_online"):
            return {"ok": False, "error": "Activa «Buscar letras en LRCLIB» en Configuración › Música."}
        it = self._songs_by_id([id]).get(str(id))
        if not it:
            return {"ok": False, "error": "Ese tema ya no está."}
        p = tagclean.propose({"name": it["name"], "title": it.get("title") or "", "artist": it.get("artist") or "",
                              "album": it.get("album") or ""}) or {}
        title = p.get("title") or it.get("title") or Path(it["name"]).stem
        artist = p.get("artist") or it.get("artist") or ""
        try:
            hit = lyrics.lookup(title, artist, it.get("album") or "", it.get("duration"))
        except lyrics.Busy as exc:
            return {"ok": False, "error": f"LRCLIB pidió esperar {exc.retry_after} s."}
        except lyrics.LyricsError as exc:
            return {"ok": False, "error": str(exc)}
        if not hit:
            return {"ok": True, "found": False}
        if hit["instrumental"] and not (hit["lrc"] or hit["plain"]):
            return {"ok": True, "found": True, "synced": [], "plain": "", "instrumental": True}
        text = hit["lrc"] or hit["plain"]
        try:
            lyrics.save(Path(it["path"]), text, bool(s.get("lyrics_beside", True)))
        except OSError as exc:  # a folder that can't be written: Bakin's copy instead
            log.info("lyrics next to the song failed (%s): kept in Bakin", exc.__class__.__name__)
            lyrics.save(Path(it["path"]), text, False)
        return {"ok": True, "found": True, **lyrics.parse_lrc(text)}

    # ---------------------------------------------- the data folder (0.18.4)

    @staticmethod
    def data_dir_info() -> dict:
        from .paths import default_dir, redirect_missing
        return {"path": str(app_dir()), "default": str(default_dir()), "missing": redirect_missing()}

    def data_dir_move(self, dest: str) -> dict:
        """Copies Bakin's data to `dest`, checks the copy, and points Bakin there (redirect.txt in
        the usual folder); the old copy stays until Bakin restarts on the new one. The downloads
        folder is not touched («Mover descargas» does that)."""
        import shutil
        from .paths import REDIRECT, default_dir
        src = app_dir()
        target = Path(str(dest or "")).expanduser()
        if not target.is_absolute():
            return {"ok": False, "error": "Usa una ruta completa."}
        if target.exists() and any(target.iterdir()):
            target = target / "BakinData"
        if target.exists() and any(target.iterdir()):
            return {"ok": False, "error": "Esa carpeta no está vacía."}
        try:
            if target.resolve().is_relative_to(src.resolve()) or src.resolve().is_relative_to(target.resolve()):
                return {"ok": False, "error": "La carpeta nueva no puede estar dentro de la actual (ni al revés)."}
        except OSError:
            pass
        skip = shutil.ignore_patterns(REDIRECT, "*.tmp", "run-*")  # plain cookie copies never travel
        try:
            shutil.copytree(src, target, ignore=skip)
        except OSError as exc:
            shutil.rmtree(target, ignore_errors=True)
            return {"ok": False, "error": f"No se pudo copiar: {exc}"}
        def count(d):
            return sum(1 for p in d.rglob("*") if p.is_file() and p.name != REDIRECT
                       and not p.name.endswith(".tmp") and not p.name.startswith("run-"))
        if count(target) < count(src):
            shutil.rmtree(target, ignore_errors=True)
            return {"ok": False, "error": "La copia quedó incompleta: no se cambió nada."}
        (default_dir() / REDIRECT).write_text(str(target), encoding="utf-8")
        log.info("data folder moved to another place; restart pending")
        return {"ok": True, "path": str(target), "restart": True}

    # ------------------------------------------------- backups (0.18.1)

    @staticmethod
    def backup_export(dest: str) -> dict:
        """Configuración › General › «Exportar…»: playlists, plays, favourites and tags, presets,
        follows, lyrics, settings in a zip. Never cookies, keys or logs."""
        from . import backup
        try:
            return {"ok": True, **backup.export(Path(dest), _library_root(None))}
        except (backup.BackupError, OSError) as exc:
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def backup_inspect(path: str) -> dict:
        from . import backup
        try:
            return {"ok": True, **backup.inspect(Path(path))}
        except (backup.BackupError, OSError) as exc:
            return {"ok": False, "error": str(exc)}

    def backup_import(self, path: str) -> dict:
        """Merges a backup in (nothing is deleted); a copy of today's data is kept first."""
        from . import backup
        try:
            res = backup.import_(Path(path), "merge", _library_root(None))
        except (backup.BackupError, OSError) as exc:
            return {"ok": False, "error": str(exc)}
        self._lib_invalidate()
        return {"ok": True, **res}

    def music_played(self, item_id: str) -> dict:
        from . import music
        return {"ok": True, "plays": music.record_play(item_id)["n"]}

    def music_state_get(self) -> dict:
        from . import music
        return music.load_state()

    def music_state_set(self, state: dict) -> dict:
        from . import music
        return music.save_state(state)

    def library_items(self, filters: dict | None = None, offset: int = 0, limit: int = 48) -> dict:
        from . import library
        found = self._query(filters)
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 120))
        page = []
        for it in found[offset:offset + limit]:
            # a file of the preview cache, loaded through asset: (PLAN 0.17.1), not base64
            page.append({**it, "thumb": library.thumb_file(it) if it["kind"] == "image"
                         else library.thumb_file(it, 96) if it["kind"] == "audio" else ""})  # a row's cover
        return {"total": len(found), "offset": offset, "items": page}

    def library_set_favorite(self, ids: list, value: bool) -> dict:
        from . import library
        n = 0
        for col, rels in self._lib_split(ids).items():
            meta = library.Meta(col)
            for rel in rels:
                meta.update(rel, fav=bool(value))
                n += 1
            meta.save()
        self._lib_refresh_meta()
        return {"ok": True, "changed": n}

    def library_tag(self, ids: list, add: list | None = None, remove: list | None = None) -> dict:
        from . import library
        add = [t for t in (library.clean_tag(x) for x in add or []) if t]
        drop = {library.clean_tag(x).lower() for x in remove or []}
        for col, rels in self._lib_split(ids).items():
            meta = library.Meta(col)
            for rel in rels:
                tags = [t for t in meta.get(rel).get("tags", []) if t.lower() not in drop]
                for t in add:
                    if t.lower() not in {x.lower() for x in tags}:
                        tags.append(t)
                meta.update(rel, tags=tags[:library.MAX_TAGS])
            meta.save()
        self._lib_refresh_meta()
        return {"ok": True}

    def _lib_refresh_meta(self) -> None:
        """Favourites/tags changed: re-read them without walking the disk again."""
        from . import library
        with self._lib_lock:
            c = self._lib_cache
            if not c:
                return
            for cid, col in c["cols"].items():
                meta = library.Meta(col)
                for it in c["items"][cid]:
                    m = meta.get(it["rel"])
                    it["fav"], it["tags"] = bool(m.get("fav")), list(m.get("tags") or [])

    def library_delete(self, ids: list) -> dict:
        """Send to the Windows Recycle Bin."""
        from . import library
        done, failed = 0, []
        for col, rels in self._lib_split(ids).items():
            ok, bad = library.delete_items(col, rels)
            done += len(ok)
            failed += [{"name": f["rel"], "reason": f["reason"]} for f in bad]
        self._lib_invalidate()
        msg = f"{done} enviado{'s' if done != 1 else ''} a la Papelera de Windows."
        if failed:
            msg += f" {len(failed)} no: {failed[0]['reason']}."
        return {"ok": not failed, "deleted": done, "failed": failed, "message": msg}

    def library_deleted(self) -> dict:
        from . import library
        idx = self._lib_index()
        out = []
        bins: dict[str, dict] = {}  # each drive's Recycle Bin, read once
        for cid, col in idx["cols"].items():
            meta = library.Meta(col)
            for rel, rec in meta.deleted.items():
                original = Path(rec.get("path") or col.root / rel)
                drive = original.drive
                if drive not in bins:
                    bins[drive] = library.recycle_bin_index(library.bin_root_of(original)) if drive else {}
                out.append({"id": f"{cid}|{rel}", "col": cid, "collection": col.name, "rel": rel,
                            "name": Path(rel).name, "kind": library.kind_of(Path(rel).suffix[1:]),
                            "size": rec.get("size"), "deleted_at": rec.get("at"),
                            "in_recycle_bin": str(original).lower() in bins[drive]})
        out.sort(key=lambda r: r["deleted_at"] or "", reverse=True)
        return {"items": out}

    def library_restore(self, ids: list) -> dict:
        from . import library
        done, failed = 0, []
        for col, rels in self._lib_split(ids).items():
            meta = library.Meta(col)
            back = set()
            for rel in rels:
                rec = meta.deleted.get(rel)
                target = library.resolve(col, rel)
                if rec is None or target is None:
                    continue
                try:
                    library.restore_from_recycle_bin(target)
                except (library.RecycleError, OSError) as exc:
                    failed.append({"name": Path(rel).name, "reason": str(exc)})
                    continue
                meta.deleted.pop(rel)
                back.add(rel)
            if back:
                meta.save()
                done += len(back)
        self._lib_invalidate()
        msg = f"{done} restaurado{'s' if done != 1 else ''}."
        if failed:
            msg += f" {len(failed)} no: {failed[0]['reason']}."
        return {"ok": not failed, "restored": done, "failed": failed, "message": msg}

    def library_forget(self, ids: list, redownload: bool = False) -> dict:
        """Drop deleted entries from the list."""
        from . import library
        n = 0
        for col, rels in self._lib_split(ids).items():
            meta = library.Meta(col)
            gone = {r for r in rels if r in meta.deleted}
            for r in gone:
                meta.deleted.pop(r)
            if gone:
                meta.save()
                n += len(gone)
        return {"ok": True, "changed": n}

    def library_add_folder(self, path: str) -> dict:
        from . import library
        p = Path(str(path or "").strip()).expanduser()
        p = p.resolve() if p.is_absolute() else p
        why = library.check_new_folder(p, _library_root(None))
        if why:
            return {"ok": False, "error": why}
        library.save_user_folders(library.user_folders() + [p])
        self._lib_invalidate()
        return {"ok": True, "id": library.folder_id(p)}

    def library_remove_folder(self, collection: str) -> dict:
        """Stop showing a folder. Its files are not touched."""
        from . import library
        keep = [p for p in library.user_folders() if library.folder_id(p) != str(collection)]
        library.save_user_folders(keep)
        self._lib_invalidate()
        return {"ok": True}

    def _lib_path(self, item_id: str) -> Path | None:
        from . import library
        cid, _, rel = str(item_id).partition("|")
        col = self._lib_index()["cols"].get(cid)
        p = library.resolve(col, rel) if col else None
        return p if p and p.is_file() else None

    def library_open(self, item_id: str) -> bool:
        p = self._lib_path(item_id)
        if p:
            os.startfile(p)  # noqa: S606
        return bool(p)

    def library_reveal(self, item_id: str) -> bool:
        import subprocess
        p = self._lib_path(item_id)
        if p:
            subprocess.Popen(["explorer.exe", "/select,", str(p)])  # noqa: S603
        return bool(p)

    # ------------------------------------------------- download folder change
    # Changing the folder can carry the downloaded accounts along (see
    # relocate.py) and is remembered, so it can be undone later.

    def _last_move(self) -> dict | None:
        from . import relocate
        rec = relocate.last_move()
        if not rec:
            return None
        return {"from": rec["from"], "to": rec["to"], "profiles": len(rec.get("folders") or [])}

    def set_out_dir(self, path: str, move: bool | None = None) -> dict:
        """move=None: if the current folder has downloads, ask first (returns
        "ask" with a summary and changes nothing). False: only switch folders.
        True: move the downloaded accounts there (in the background)."""
        from . import relocate
        text = str(path or "").strip()
        if not text:
            return {"ok": False, "error": "Elige una carpeta."}
        new = Path(text).expanduser()
        if not new.is_absolute():
            return {"ok": False, "error": "Usa una ruta completa, por ejemplo C:\\Users\\tu\\Downloads."}
        new = new.resolve()
        settings = load_settings()
        old = Path(settings["out_dir"]).expanduser().resolve()
        if new == old:
            return {"ok": True, "out_dir": str(new), "changed": False}
        if is_inside(new, app_dir()):
            return {"ok": False, "error": "La carpeta de descargas no puede estar dentro de los datos privados de Bakin."}
        found = relocate.accounts(old)
        if move is None and found:
            files = size = 0
            for d in found:
                f, b = relocate.folder_size(d)
                files, size = files + f, size + b
            return {"ok": False, "ask": True, "from": str(old), "to": str(new),
                    "profiles": [d.name for d in found], "files": files, "bytes": size}
        if not move or not found:
            self._save_out_dir(new)
            relocate.save_move(relocate.new_record(old, new, []))
            return {"ok": True, "out_dir": str(new), "changed": True, "last_move": self._last_move()}
        why = relocate.check_target(old, new)
        if why:
            return {"ok": False, "error": why}
        with self._lock:
            if self._moving:
                return {"ok": False, "error": "Ya se están moviendo las descargas."}
            if self._current or self._queue:
                return {"ok": False, "error": "Espera a que terminen las descargas (y la cola) para moverlas."}
            self._moving = True
        threading.Thread(target=self._move, args=(old, new, None, False),
                         name="bakin-move", daemon=True).start()
        return {"ok": True, "moving": True}

    def undo_move(self) -> dict:
        """Go back to the previous folder, bringing back what was moved."""
        from . import relocate
        rec = relocate.last_move()
        if not rec:
            return {"ok": False, "error": "No hay ningún cambio de carpeta para deshacer."}
        src, dst = Path(rec["to"]), Path(rec["from"])
        folders = list(rec.get("folders") or [])
        if not folders:
            self._save_out_dir(dst)
            relocate.save_move(None)
            return {"ok": True, "out_dir": str(dst), "last_move": None}
        with self._lock:
            if self._moving:
                return {"ok": False, "error": "Ya se están moviendo las descargas."}
            if self._current or self._queue:
                return {"ok": False, "error": "Espera a que terminen las descargas (y la cola) para deshacer."}
            self._moving = True
        threading.Thread(target=self._move, args=(src, dst, folders, True),
                         name="bakin-move", daemon=True).start()
        return {"ok": True, "moving": True}

    def _save_out_dir(self, path: Path) -> None:
        settings = load_settings()
        settings["out_dir"] = str(path)
        save_settings(settings)

    def _move(self, src: Path, dst: Path, folders: list[str] | None, undo: bool) -> None:
        from . import relocate
        try:
            res = relocate.move_accounts(src, dst, folders,
                                         progress=lambda p: self._push({"kind": "move", "undo": undo, **p}))
            self._save_out_dir(dst)
            if undo:
                left = [f for f in (folders or []) if f not in res.moved]
                # Whatever could not come back stays recorded, so undo can be retried.
                relocate.save_move(relocate.new_record(dst, src, left) if left else None)
            else:
                relocate.save_move(relocate.new_record(src, dst, res.moved))
            n = len(res.moved)
            message = (f"{'Devueltos' if undo else 'Movidos'} {n} perfil{'es' if n != 1 else ''} "
                       f"a {dst}.")
            if res.skipped:
                message += " No se movieron: " + "; ".join(f"{s['folder']} ({s['reason']})" for s in res.skipped) + \
                    f". Siguen en {src}."
            payload = {"ok": not res.skipped, "moved": res.moved, "skipped": res.skipped,
                       "message": message, "out_dir": str(dst)}
        except Exception as exc:
            log.error("move failed:\n%s", redact.format_exception_redacted(exc))
            payload = {"ok": False, "moved": [], "skipped": [], "out_dir": str(load_settings()["out_dir"]),
                       "message": f"No se pudieron mover las descargas: {exc}. Los originales no se tocaron."}
        finally:
            with self._lock:
                self._moving = False
        self._lib_invalidate()
        self._push({"kind": "move_done", "undo": undo, "last_move": self._last_move(), **payload})

    # ---------------------------------------------------------------- jobs

    def start(self, mode: str, url: str, options: dict | None = None) -> dict:
        result = self._start(mode, url, options)
        if result.get("media"):  # tell the window the queue changed
            self._push_queue()
        return result

    def _start(self, mode: str, url: str, options: dict | None = None) -> dict:
        with self._lock:
            if self._moving:
                return {"ok": False, "error": "Se están moviendo las descargas: espera a que termine."}
            if mode not in ("analyze", "download"):
                return {"ok": False, "error": "modo inválido"}
            from . import wallhaven
            wh = wallhaven.parse_url(str(url or ""))
            if wh:
                return self._start_wallhaven(wh, options or {})
            if _is_web_url(url):
                return self._start_media(str(url).strip(), options or {})
            return {"ok": False, "field": "url", "error":
                    "Eso no es un enlace válido. Pega un enlace http(s): Wallhaven, YouTube, SoundCloud…"}

    def _run_expand(self, item: dict) -> str:
        """A private list (YouTube Music «Me gusta»): read its songs once with
        the cookies, then queue each one as a public video, so downloading
        doesn't go through the account (cookies stay for age-gated songs)."""
        from . import mediadl
        ms = mediadl.load_settings()
        lp = next((p for p in ms["presets"] if p["id"] == item["list_preset"]), None)
        cp = next((p for p in ms["presets"] if p["id"] == item["child_preset"]), None)
        self._push({"kind": "start", "mode": "media", "target_kind": "media", "title": item["title"],
                    "message": "Leyendo la lista (una sola vez, con tus cookies)…"})
        if not lp or not cp:
            self._push({"kind": "finished", "mode": "media", "outcome": "error", "message": "Falta un preset de la lista.",
                        "stats": None, "dir": None})
            return "error"
        try:
            ids = mediadl.list_ids(item["url"], lp, self._cancel)
        except mediadl.MediaError as exc:
            self._push({"kind": "finished", "mode": "media", "outcome": "error", "message": str(exc), "stats": None, "dir": None})
            return "error"
        done = mediadl.archived_ids(mediadl.destination(cp, item["root"]))
        new = [i for i in ids if i not in done]
        with self._lock:
            for vid in new:
                self._next_id += 1
                self._queue.append({"id": self._next_id, "kind": "media", "mode": "download",
                                    "url": f"https://www.youtube.com/watch?v={vid}", "preset": cp,
                                    "root": item["root"], "retry_on_partial": False, "replaces": None,
                                    "title": f"{item['title']}: {vid}", "opts": {"max_posts": 0}})
            self._dirty = True
        msg = f"La lista tiene {len(ids)} canciones; {len(ids) - len(new)} ya estaban. {len(new)} en cola, sin usar tu cuenta."
        self._push({"kind": "finished", "mode": "media", "outcome": "ok", "message": msg, "stats": None, "dir": None})
        self._push_queue()
        return "ok"

    def _start_media(self, url: str, options: dict, *, retry_on_partial: bool = False,
                     title: str | None = None, preset_obj: dict | None = None,
                     replaces: dict | None = None) -> dict:
        """Queue a yt-dlp download. Called with self._lock held."""
        from . import mediadl, tools
        if self._moving:
            return {"ok": False, "error": "Se están moviendo las descargas: espera a que termine."}
        missing = tools.missing()
        if missing:
            return {"ok": False, "field": "tools", "missing": missing,
                    "error": "Para música y video Bakin necesita " + ", ".join(missing) + ". Instálalas desde «Herramientas»."}
        ms = mediadl.load_settings()
        pid = str(options.get("preset") or ms["site_prefs"].get(mediadl.site_of(url)) or ms["default_preset"])
        preset = preset_obj or next((p for p in ms["presets"] if p["id"] == pid), None)
        if preset is None:
            return {"ok": False, "error": "Ese preset ya no existe."}
        # One-off choices made in the download dialog (format, quality...),
        # without changing the saved preset.
        override = {k: v for k, v in (options.get("override") or {}).items()
                    if k in ("mode", "audio_format", "audio_quality", "video_max_height",
                             "playlist_items", "section", "split_chapters", "subtitles")}
        if override:
            preset = mediadl.clean_preset({**preset, **override})
        root = _library_root(None)
        why = mediadl.check_folder(preset, root)
        if why:
            return {"ok": False, "error": why}
        if mediadl.is_private_list(url) and not preset_obj:
            child = "bimusic" if any(p["id"] == "bimusic" for p in ms["presets"]) else preset["id"]
            self._next_id += 1
            self._queue.append({"id": self._next_id, "kind": "expand", "mode": "download", "url": url,
                                "list_preset": preset["id"], "child_preset": child, "root": root,
                                "title": "Me gusta de YouTube Music", "opts": {"max_posts": 0}})
            waiting = len(self._queue) - (0 if self._current or self._paused else 1)
            self._ensure_worker()
            return {"ok": True, "waiting": waiting, "media": True}
        for item in ([self._current] if self._current else []) + self._queue:
            if item.get("kind") == "media" and item["url"] == url and all(
                    item["preset"][k] == preset[k] for k in ("id", "mode", "audio_format")):
                return {"ok": False, "error": "Ese enlace ya está en la cola con ese preset."}
        if len(self._queue) >= QUEUE_LIMIT:
            return {"ok": False, "error": f"La cola ya tiene {QUEUE_LIMIT} trabajos."}
        self._next_id += 1
        short = re.sub(r"^https?://(www\.)?", "", url)[:60]
        self._queue.append({"id": self._next_id, "kind": "media", "mode": "download", "url": url,
                            "preset": preset, "root": root, "retry_on_partial": retry_on_partial,
                            "title": title or f"{short} · {preset['name']} "
                                     f"({preset['audio_format'].upper() if preset['mode'] == 'audio' else 'Video'})",
                            "opts": {"max_posts": 0}, "replaces": replaces})
        waiting = len(self._queue) - (0 if self._current or self._paused else 1)
        self._ensure_worker()
        return {"ok": True, "waiting": waiting, "media": True}

    def cancel(self) -> None:
        """Stop the running job and drop everything waiting behind it."""
        with self._lock:
            self._queue.clear()
            self._paused, self._paused_until, self._pause_reason = False, None, ""
            self._cancel.set()
        self._push_queue()

    def remove_queued(self, item_id: int) -> dict:
        with self._lock:
            before = len(self._queue)
            self._queue = [i for i in self._queue if i["id"] != int(item_id)]
            removed = len(self._queue) < before
            if not self._queue:
                self._paused = False
        self._push_queue()
        return {"ok": removed}

    def resume_queue(self) -> dict:
        with self._lock:
            self._holds.clear()
            self._dirty = True
            self._ensure_worker()
        self._push_queue()
        return {"ok": True}

    def _queue_state(self) -> dict:
        def row(item, status):
            return {"id": item["id"], "title": item["title"], "mode": item["mode"], "status": status,
                    "kind": item.get("kind"), "lane": self._lane_of(item)}
        # a whole music library can be queued: send each lane's first ones, not all of it
        per: dict[str, list] = {lane: [] for lane in LANES}
        counts = dict.fromkeys(LANES, 0)
        for i in self._queue:
            lane = self._lane_of(i)
            counts[lane] += 1
            if len(per[lane]) < 50:
                per[lane].append(i)
        items = ([row(self._current, "running")] if self._current else []) + \
            [row(i, "pending" if self._runnable(i) else "paused") for lane in LANES for i in per[lane]]
        running = self._lane_of(self._current) if self._current else None
        lanes = [{"id": lane, "name": LANE_NAMES[lane], "count": counts[lane] + (running == lane), "running": running == lane,
                  "held": self._holds.get(lane)} for lane in LANES]
        limit, used = self._budget()
        return {"items": items, "lanes": lanes, "paused": self._paused, "total": len(self._queue) + bool(self._current),
                "paused_until": self._paused_until, "reason": self._pause_reason,
                "scope": getattr(self, "_pause_scope", "all") if self._paused else None,
                "daily_limit": limit, "downloads_today": used}

    def _push_queue(self) -> None:
        with self._lock:
            state = self._queue_state()
            self._dirty = True
        self._push({"kind": "queue", **state})

    def _ensure_worker(self) -> None:
        """Start the worker if there is work and it is not running. Hold self._lock."""
        if self._queue and self._next_index() is not None and not (self._worker and self._worker.is_alive()):
            self._worker = threading.Thread(target=self._work, name="bakin-job", daemon=True)
            self._worker.start()

    def _work(self) -> None:
        while True:
            with self._lock:
                idx = self._next_index() if not self._closing else None
                if idx is None:
                    self._current = None
                    break
                nxt = self._queue[idx]
            if nxt.get("kind") != "convert":
                wait = self._outside_hours()
                if wait:
                    self.pause_queue(wait[0], until=wait[1], scope="downloads")  # conversions are local: they go on
                    continue
            if nxt.get("kind") in ("media", "expand") and nxt.get("counts", True):
                limit, used = self._budget()
                if limit and used >= limit:
                    self._pause_until_tomorrow()
                    continue
            with self._lock:
                idx = self._next_index() if not self._closing else None
                if idx is None:
                    self._current = None
                    break
                item = self._current = self._queue.pop(idx)
                self._cancel = threading.Event()
                self._photos.clear()
                self._job_dir = None
            self._push_queue()
            if item.get("kind") == "media":
                outcome = self._run_media(item)
            elif item.get("kind") == "expand":
                outcome = self._run_expand(item)
            elif item.get("kind") == "convert":
                outcome = self._run_convert(item)
            else:
                outcome = self._run_wallhaven(item)
            with self._lock:
                self._current = None
                if self._closing:
                    break
                if outcome == "requeue":  # the daily limit ran out in the middle of a list
                    self._queue.insert(0, item)
                if outcome == "stopped" and self._queue and self._lane_of(item) not in self._holds:
                    # the site asked us to stop: do not hit it again (that lane only)
                    self._hold([self._lane_of(item)], "El sitio pidió parar: Reanudar cuando quieras.", None)
                self._dirty = True
        self._push_queue()

    # ------------------------------------------- better copies of old music

    def library_upgrade_audio(self, ids: list, preset_id: str, audio_format: str = "opus") -> dict:
        """Download again, from its source, each selected song in a better
        format (not a conversion). The old file goes to the Recycle Bin only
        once the new one is saved; if the source is gone, it is kept."""
        from . import library, mediadl, tools
        if audio_format not in ("opus", "best", "m4a", "mp3", "flac"):
            return {"ok": False, "error": "Formato inválido."}
        missing = tools.missing()
        if missing:
            return {"ok": False, "field": "tools", "missing": missing,
                    "error": "Hace falta " + ", ".join(missing) + ": instálalas desde «Herramientas»."}
        ms = mediadl.load_settings()
        base = next((p for p in ms["presets"] if p["id"] == preset_id), None) or \
            next(p for p in ms["presets"] if p["id"] == ms["default_preset"])
        idx = self._lib_index()
        by_id = {it["id"]: it for items in idx["items"].values() for it in items}
        queued, no_id, already, refused = 0, [], [], []
        with self._lock:
            for raw in ids or []:
                it = by_id.get(str(raw))
                if not it or it["kind"] != "audio":
                    continue
                if it.get("codec") == audio_format or (audio_format == "best" and it.get("codec") == "opus") \
                        or it.get("lossless"):
                    already.append(it["name"])
                    continue
                if not it.get("yt_id"):
                    no_id.append(it["name"])
                    continue
                old = Path(it["path"])
                stem = old.stem.replace("%", "%%")
                preset = mediadl.clean_preset({**base, "id": base["id"], "mode": "audio", "audio_format": audio_format,
                                               "audio_quality": "0", "folder": str(old.parent),
                                               "template": stem + ".%(ext)s", "archive": False,
                                               "playlist_items": ""})
                r = self._start_media(f"https://www.youtube.com/watch?v={it['yt_id']}", {},
                                      preset_obj=preset, retry_on_partial=True,
                                      title=f"Mejorar: {old.name} → {'audio original' if audio_format == 'best' else audio_format.upper()}",
                                      replaces={"id": it["id"], "path": str(old), "yt_id": it["yt_id"]})
                if r.get("ok"):
                    queued += 1
                else:
                    refused.append(f"{it['name']}: {r.get('error')}")
        self._push_queue()
        return {"ok": True, "queued": queued, "no_id": no_id, "already": already, "refused": refused}

    def _finish_upgrade(self, replaces: dict, stats) -> None:
        from . import library
        old = Path(replaces["path"])
        new = next((Path(f) for f in (stats.files if stats else []) if Path(f).is_file() and Path(f) != old), None)
        if new is None:
            self._push({"kind": "warn", "message": f"{old.name}: no se pudo bajar de nuevo; se conserva el archivo actual."})
            return
        cid, _, old_rel = replaces["id"].partition("|")
        col = self._lib_index()["cols"].get(cid)
        if col is None or not old.is_file():
            return
        meta = library.Meta(col)
        kept = meta.get(old_rel)
        new_rel = new.relative_to(col.root).as_posix()
        if kept:  # favourites and tags follow the song to its new file
            meta.update(new_rel, **{k: kept[k] for k in ("fav", "tags") if k in kept})
            meta.save()
        from . import music
        music.move_plays(replaces["id"], f"{cid}|{new_rel}")  # and so do its plays
        music.rename_ids(replaces["id"], f"{cid}|{new_rel}")  # and its place in playlists
        try:
            library.delete_items(col, [old_rel])
            self._push({"kind": "info", "message": f"{old.name} → {new.name}; el anterior está en «Eliminadas» (Papelera)."})
        except Exception as exc:
            self._push({"kind": "warn", "message": f"Se bajó {new.name}, pero no se pudo mandar {old.name} a la Papelera: {exc}"})

    def _join_preset_folder(self, out_dir: Path, root: Path) -> None:
        """A preset folder outside the download folder joins the library as a folder of its own."""
        from . import library
        if out_dir.is_dir() and not out_dir.resolve().is_relative_to(root.resolve()):
            if library.check_new_folder(out_dir.resolve(), root) is None:
                library.save_user_folders(library.user_folders() + [out_dir.resolve()])

    def _run_media(self, item: dict) -> str:
        from . import mediadl
        preset, url = item["preset"], item["url"]
        self._push({"kind": "start", "mode": "media", "target_kind": "media", "title": item["title"],
                    "message": f"Descargando {url} con «{preset['name']}»"})
        ms = mediadl.load_settings()
        out_dir = mediadl.destination(preset, item["root"])
        outcome, message, stats = "ok", "", None
        try:
            limit, used = self._budget()
            budget = max(1, limit - used) if limit and item.get("counts", True) else None
            joined = False

            def emit(ev: dict) -> None:
                # each song as it lands (a friend's report: a long list showed nothing until Bakin
                # restarted; the library was only read again when the whole job ended)
                nonlocal joined
                if ev.get("kind") == "media_file":
                    if not joined and not item.get("replaces"):
                        joined = True
                        self._join_preset_folder(out_dir, item["root"])
                    self._lib_invalidate()
                self._push(ev)

            stats = mediadl.download([url], preset, item["root"], emit, self._cancel,
                                     verbose=ms["verbose"],
                                     retry_on_partial=item["retry_on_partial"], mark=not item.get("replaces"),
                                     max_downloads=budget)
            self._count_downloads(stats.done)
            if item.get("replaces"):
                self._finish_upgrade(item["replaces"], stats)
            if not item.get("replaces"):
                self._join_preset_folder(out_dir, item["root"])
            if self._cancel.is_set():
                outcome, message = "cancelled", "Cancelado. Lo terminado se conserva; los archivos a medias se retoman la próxima vez."
            else:
                parts = [f"{stats.done} descargado{'s' if stats.done != 1 else ''}"]
                if stats.skipped:
                    parts.append(f"{stats.skipped} ya estaba{'n' if stats.skipped != 1 else ''} (archive)")
                if stats.failed:
                    parts.append(f"{stats.failed} con error")
                outcome = "error" if stats.failed and not stats.done and not stats.skipped else "ok"
                message = "Listo: " + ", ".join(parts) + "."
                if stats.errors:  # which one and why, not just «con error»
                    who = f"«{item['title']}»" if item.get("title") else "Un tema"
                    message += f" {who} no se pudo bajar: {stats.errors[0]}"
                if stats.blocked:
                    import time as _t
                    hours = 6
                    self.pause_queue(f"YouTube frenó las descargas («confirma que no eres un bot» o 429). "
                                     f"Se reintenta solo en {hours} horas.", until=_t.time() + hours * 3600, scope="media")
                    with self._lock:
                        self._queue.insert(0, item)  # this link didn't get its turn: retry it first
                    outcome = "paused"
                    message = f"YouTube frenó las descargas: la cola se reanuda sola en {hours} horas."
                    # what fixed it every time: a newer yt-dlp (checked now; the retry uses it)
                    self._ytdlp_checked_at = 0
                    self._ytdlp_force = True
                elif stats.limit_reached:
                    outcome = "requeue"
                    message = "Se llegó al límite diario a mitad de la lista: sigue mañana desde donde quedó."
            if stats.cookies_bad and preset["cookies_mode"] != "off":
                self._mark_cookies_bad(preset)
            if preset["mode"] == "audio" and not self._cancel.is_set():
                left = mediadl.leftover_videos(out_dir)
                if left:
                    self._push({"kind": "media_leftovers", "preset": preset["id"], "folder": str(out_dir),
                                "items": left[:50], "count": len(left)})
        except mediadl.MediaError as exc:
            outcome, message = "error", str(exc)
        except Exception as exc:
            log.error("media job failed:\n%s", redact.format_exception_redacted(exc))
            outcome, message = "error", f"Error inesperado: {exc.__class__.__name__}: {exc}"
        self._follow_history(item["url"], {
            "at": time.time(), "outcome": outcome, "new": stats.done if stats else 0,
            "skipped": stats.skipped if stats else 0,
            "failed": [redact.redact(mediadl.friendly_error(e))[:200] for e in (stats.errors if stats else [message])[:5] if e]})
        self._job_dir = out_dir if out_dir.is_dir() else None
        self._lib_invalidate()
        self._push({"kind": "finished", "mode": "media", "outcome": outcome, "message": redact.redact(message),
                    "stats": None, "media_stats": stats.as_dict() if stats else None, "lane": "media",
                    "count": stats.done if stats else 0, "skipped": stats.skipped if stats else 0,
                    "dir": str(out_dir) if out_dir.is_dir() else None})
        return outcome

    # ------------------------------------------------------------- wallhaven
    # Wallpapers from wallhaven.cc (bakin/wallhaven.py): a search to preview
    # in the dialog, then a queued job that downloads them with their tags.

    def wallhaven_search(self, filters: dict | None = None, page: int = 1) -> dict:
        from . import wallhaven
        # The UI test answers from a saved page of results instead of the network
        # (never outside a test copy: BAKIN_HOME must be set too).
        fake = os.environ.get("BAKIN_FAKE_WALLHAVEN")
        if fake and os.environ.get("BAKIN_HOME"):
            try:
                return {"ok": True, **json.loads(Path(fake).read_text(encoding="utf-8"))}
            except (OSError, ValueError) as exc:
                return {"ok": False, "error": f"búsqueda de prueba: {exc}"}
        try:
            res = wallhaven.search(filters or {}, page)
        except wallhaven.WallhavenError as exc:
            return {"ok": False, "error": str(exc)}
        root = _library_root(None)
        have = wallhaven.archived(wallhaven.destination(root))
        for it in res["items"]:
            it["have"] = it["id"] in have
        return {"ok": True, **res}

    # The optional API key (Windows Credential Manager, never a file or the page)
    # and the user name it goes with (not secret: in ui-settings).

    def wallhaven_account(self) -> dict:
        from . import wallhaven
        return {"key": bool(wallhaven.api_key()), "user": load_settings().get("wallhaven_user", "")}

    def wallhaven_account_set(self, key: str = "", user: str = "") -> dict:
        from . import credstore, wallhaven
        key, user = str(key or "").strip(), str(user or "").strip()
        if user and not re.fullmatch(r"[\w.-]{1,40}", user):
            return {"ok": False, "field": "user", "error": "Ese no es un usuario de Wallhaven."}
        if key:
            try:
                wallhaven.check_key(key)
            except wallhaven.WallhavenError as exc:
                return {"ok": False, "field": "key", "error": str(exc)}
            try:
                credstore.put(wallhaven.key_target(), key)
            except OSError as exc:
                return {"ok": False, "field": "key", "error": str(exc)}
            wallhaven.forget_key_cache()
        save_settings({**load_settings(), "wallhaven_user": user})
        return {"ok": True, **self.wallhaven_account()}

    def wallhaven_account_clear(self) -> dict:
        from . import credstore, wallhaven
        try:
            credstore.delete(wallhaven.key_target())
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        wallhaven.forget_key_cache()
        return {"ok": True, **self.wallhaven_account()}

    def wallhaven_my_collections(self) -> dict:
        from . import wallhaven
        user = load_settings().get("wallhaven_user", "")
        try:
            cols = wallhaven.my_collections()
        except wallhaven.WallhavenError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "user": user, "collections": cols}

    def wallhaven_follow(self, job: dict | None = None, every_hours: int = 24) -> dict:
        """Follow a search or a collection: every few hours what is new goes to
        its own folder (Wallhaven/<name>), also with the window closed."""
        import uuid as _uuid
        job = dict(job or {})
        if job.get("type") not in ("search", "collection"):
            return {"ok": False, "error": "Solo se puede seguir una búsqueda o una colección."}
        clean = self._clean_wallhaven_job(job)
        if "error" in clean:
            return {"ok": False, "error": clean["error"]}
        try:
            every = max(1, min(24 * 30, int(every_hours)))
        except (TypeError, ValueError):
            every = 24
        follows = self._load_follows()
        if any(f.get("kind") == "wallhaven" and f.get("job") == clean for f in follows):
            return {"ok": False, "error": "Ya la sigues."}
        entry = {"id": _uuid.uuid4().hex[:10], "kind": "wallhaven", "job": clean, "url": f"wallhaven:{clean['label']}",
                 "title": f"Wallhaven: {clean['label']}", "every_hours": every, "last_check": 0, "enabled": True}
        follows.append(entry)
        self._save_follows(follows)
        r = self._queue_follow(entry)  # the first ones now
        return {"ok": True, "follow": entry, "queued": bool(r.get("ok"))}

    def wallhaven_collection(self, user: str = "", collection_id: str = "", page: int = 1, sketchy: bool = False) -> dict:
        """One collection's wallpapers, to pick from like a search's (private ones need the key)."""
        from . import wallhaven
        try:
            res = wallhaven.collection(str(user or ""), str(collection_id or ""), page, sketchy=bool(sketchy))
        except wallhaven.WallhavenError as exc:
            return {"ok": False, "error": str(exc)}
        have = wallhaven.archived(wallhaven.destination(_library_root(None)))
        for it in res["items"]:
            it["have"] = it["id"] in have
        return {"ok": True, **res}

    def wallhaven_open(self, wallpaper_id: str) -> dict:
        """Opens one wallpaper's page on wallhaven.cc in the browser (nothing else)."""
        if not re.fullmatch(r"[a-z0-9]{4,10}", str(wallpaper_id or "")):
            return {"ok": False, "error": "Ese no es un fondo de Wallhaven."}
        webbrowser.open(f"https://wallhaven.cc/w/{wallpaper_id}")
        return {"ok": True}

    def music_search_open(self, q: str) -> dict:
        """Opens YouTube Music's search for some words in the browser (2026-11-06: someone typed an
        artist in Bakin's library search to find new music). Until Bakin searches by itself."""
        from urllib.parse import quote
        words = " ".join(str(q or "").split())[:120]
        if not words:
            return {"ok": False, "error": "Escribe qué buscar."}
        webbrowser.open(f"https://music.youtube.com/search?q={quote(words)}")
        return {"ok": True}

    def wallhaven_download(self, job: dict | None = None) -> dict:
        with self._lock:
            result = self._start_wallhaven(job or {}, {})
        if result.get("ok"):
            self._push_queue()
        return result

    @staticmethod
    def _clean_wallhaven_job(job: dict, options: dict | None = None) -> dict:
        """A Wallhaven job as the page or a follow asks it, checked; {"error"} if not valid."""
        from . import wallhaven
        options = options or {}
        kind = job.get("type")
        if kind == "wallpaper" and re.fullmatch(r"[a-z0-9]{4,10}", str(job.get("id") or "")):
            clean = {"type": "wallpaper", "id": job["id"]}
        elif kind == "ids":
            ids = [i for i in dict.fromkeys(str(x) for x in job.get("ids") or []) if re.fullmatch(r"[a-z0-9]{4,10}", i)]
            if not ids:
                return {"error": "Elige al menos un fondo."}
            clean = {"type": "ids", "ids": ids[:wallhaven.MAX_PER_JOB]}
        elif kind == "search":
            clean = {"type": "search", "filters": wallhaven.clean_filters(job.get("filters"))}
        elif kind == "collection" and re.fullmatch(r"[\w.-]{1,40}", str(job.get("user") or "")) and str(job.get("id") or "").isdigit():
            clean = {"type": "collection", "user": job["user"], "id": str(job["id"]),
                     "sketchy": bool(job.get("sketchy"))}
        else:
            return {"error": "Ese pedido de Wallhaven no es válido."}
        limit = int(job.get("limit") or options.get("limit") or 0) or (24 if kind in ("search", "collection") else 0)
        if limit:
            clean["limit"] = max(1, min(limit, wallhaven.MAX_PER_JOB))
        clean["label"] = wallhaven.safe_name(job.get("label") or wallhaven.label_for(clean))
        return clean

    def _start_wallhaven(self, job: dict, options: dict) -> dict:
        """Queue a Wallhaven job. Called with self._lock held."""
        from . import wallhaven
        if self._moving:
            return {"ok": False, "error": "Se están moviendo las descargas: espera a que termine."}
        clean = self._clean_wallhaven_job(job, options)
        if "error" in clean:
            return {"ok": False, "error": clean["error"]}
        # its own lane: a music library in the queue never keeps wallpapers out or waiting
        if sum(1 for q in self._queue if q.get("kind") == "wallhaven") >= WALLHAVEN_LIMIT:
            return {"ok": False, "error": f"Ya hay {WALLHAVEN_LIMIT} pedidos de Wallhaven en la cola."}
        for item in ([self._current] if self._current else []) + self._queue:
            if item.get("kind") == "wallhaven" and item["job"] == clean:
                return {"ok": False, "error": "Eso ya está en la cola."}
        n = len(clean.get("ids") or []) or clean.get("limit") or 1
        title = f"Wallhaven: {clean['label']} ({n} fondo{'s' if n != 1 else ''})"
        self._next_id += 1
        ahead = next((i for i, q in enumerate(self._queue) if q.get("kind") in ("media", "expand")), len(self._queue))
        self._queue.insert(ahead, {"id": self._next_id, "kind": "wallhaven", "mode": "download", "job": clean,
                                   "root": _library_root(None), "title": title, "opts": {"max_posts": 0}})
        # jobs before it (the running one too), and itself if a pause holds it
        waiting = ahead + bool(self._current) + (not self._runnable(self._queue[ahead]))
        self._ensure_worker()
        return {"ok": True, "waiting": waiting, "media": True}

    def _run_wallhaven(self, item: dict) -> str:
        from . import wallhaven
        self._push({"kind": "start", "mode": "wallhaven", "target_kind": "media", "title": item["title"],
                    "message": f"Bajando de Wallhaven: {item['job'].get('label', '')}"})
        outcome, message, stats = "ok", "", None
        try:
            stats = wallhaven.download(item["job"], item["root"], self._push, self._cancel)
            if self._cancel.is_set():
                outcome, message = "cancelled", "Cancelado. Los fondos ya bajados se conservan."
            else:
                parts = [f"{stats.done} fondo{'s' if stats.done != 1 else ''} nuevo{'s' if stats.done != 1 else ''}"]
                if stats.skipped:
                    parts.append(f"{stats.skipped} ya {'estaban' if stats.skipped != 1 else 'estaba'}")
                if stats.failed:
                    parts.append(f"{stats.failed} con error")
                message = "Listo: " + ", ".join(parts) + "."
                if stats.errors:
                    message += f" Uno no se pudo bajar: {stats.errors[0]}"
                outcome = "error" if stats.failed and not stats.done and not stats.skipped else "ok"
                if stats.blocked:
                    import time as _t
                    self.pause_queue("Wallhaven pidió ir más despacio (429). Se reintenta solo en una hora.",
                                     until=_t.time() + 3600, scope="wallhaven")
                    with self._lock:
                        self._queue.insert(0, item)  # the rest, later (the archive skips what is done)
                    outcome, message = "paused", "Wallhaven pidió ir más despacio: la cola sigue sola en una hora."
        except wallhaven.WallhavenError as exc:
            outcome, message = "error", str(exc)
        except Exception as exc:  # noqa: BLE001 - one job must not stop the queue
            log.exception("wallhaven job failed")
            outcome, message = "error", f"Error inesperado: {exc.__class__.__name__}: {exc}"
        self._lib_invalidate()
        dest = wallhaven.destination(item["root"])
        self._push({"kind": "finished", "mode": "media", "outcome": "ok" if outcome == "paused" else outcome,
                    "message": message, "stats": None, "lane": "wallhaven",
                    "count": stats.done if stats else 0, "skipped": stats.skipped if stats else 0,
                    "dir": str(dest) if dest.is_dir() else None})
        return outcome

    # --------------------------------------------------------------- convert
    # Images (Pillow), audio and video (ffmpeg) to another format, as new
    # files; the originals are only read.

    def convert_formats(self) -> dict:
        from . import convert
        return convert.formats()

    def _convert_input(self, ref) -> Path | None:
        """A file to convert: a library id, or a path the user picked/dropped."""
        if isinstance(ref, dict) and ref.get("id"):
            return self._lib_path(str(ref["id"]))
        raw = ref.get("path") if isinstance(ref, dict) else ref
        if not raw:
            return None
        p = Path(str(raw)).expanduser()
        if not p.is_absolute():
            return None
        p = p.resolve()
        return p if p.is_file() and not is_inside(p, app_dir()) else None

    def convert_inspect(self, items: list) -> dict:
        from . import convert
        out, skipped = [], 0
        for ref in (items or [])[:500]:
            p = self._convert_input(ref)
            if p is None:
                skipped += 1
                continue
            d = convert.describe(p)
            if d["kind"] == "image":
                from . import library
                d["thumb"] = library.thumb({"path": str(p), "ext": d["ext"], "size": d["size"], "mtime": d["mtime"]}, 160)
            out.append(d)
        return {"files": out, "skipped": skipped}

    def convert_start(self, paths: list, options: dict) -> dict:
        from . import convert
        try:
            opts = convert.clean_options(options or {})
        except convert.ConvertError as exc:
            return {"ok": False, "error": str(exc)}
        if opts["out_dir"]:
            d = Path(opts["out_dir"]).expanduser()
            if not d.is_absolute() or is_inside(d, app_dir()):
                return {"ok": False, "error": "Elige una carpeta de salida válida."}
            opts["out_dir"] = str(d.resolve())
        files = [p for p in (self._convert_input({"path": x}) for x in (paths or [])[:500]) if p]
        if not files:
            return {"ok": False, "error": "No hay archivos para convertir."}
        bad = [w for w in (convert.can_convert(f, opts["target"]) for f in files) if w]
        if len(bad) == len(files):
            return {"ok": False, "error": bad[0]}
        files = [f for f in files if convert.can_convert(f, opts["target"]) is None]  # skip, don't fail
        if convert.target_kind(opts["target"]) != "image" and tools_missing_ffmpeg():
            return {"ok": False, "field": "tools", "missing": ["ffmpeg"],
                    "error": "Para audio y video hace falta ffmpeg: instálalo desde «Herramientas»."}
        with self._lock:
            if self._moving:
                return {"ok": False, "error": "Se están moviendo las descargas: espera a que termine."}
            if sum(1 for q in self._queue if q.get("kind") == "convert") >= WALLHAVEN_LIMIT:
                return {"ok": False, "error": f"Ya hay {WALLHAVEN_LIMIT} conversiones en la cola."}
            self._next_id += 1
            n = len(files)
            title = f"Convertir {n} archivo{'s' if n != 1 else ''} a {opts['target'].upper()}"
            # the fast lane too: ahead of the music and video waiting
            ahead = next((i for i, q in enumerate(self._queue) if q.get("kind") in ("media", "expand")), len(self._queue))
            self._queue.insert(ahead, {"id": self._next_id, "kind": "convert", "mode": "convert", "files": files,
                                       "opts": {**opts, "max_posts": 0}, "title": title})
            waiting = ahead + bool(self._current) + (not self._runnable(self._queue[ahead]))
            self._ensure_worker()
        self._push_queue()
        return {"ok": True, "waiting": waiting, "skipped": len(bad)}

    def _run_convert(self, item: dict) -> str:
        from . import convert
        opts = item["opts"]
        self._push({"kind": "start", "mode": "convert", "target_kind": "convert", "title": item["title"],
                    "message": item["title"]})
        outcome, message, stats = "ok", "", None
        try:
            stats = convert.convert_files(item["files"], opts, self._push, self._cancel)
            if self._cancel.is_set():
                outcome, message = "cancelled", f"Cancelado. {stats.done} convertido{'s' if stats.done != 1 else ''}; los originales no se tocaron."
            else:
                message = f"Listo: {stats.done} convertido{'s' if stats.done != 1 else ''}"
                message += f", {stats.failed} con error." if stats.failed else "."
                outcome = "error" if stats.failed and not stats.done else "ok"
        except Exception as exc:
            log.error("convert job failed:\n%s", redact.format_exception_redacted(exc))
            outcome, message = "error", f"Error inesperado: {exc.__class__.__name__}: {exc}"
        outs = stats.outputs if stats else []
        self._job_dir = Path(outs[0]).parent if outs else None
        self._photos = {str(i): Path(o) for i, o in enumerate(outs, 1)}
        self._lib_invalidate()
        self._push({"kind": "finished", "mode": "convert", "outcome": outcome, "message": message,
                    "stats": None, "outputs": outs[:200],
                    "dir": str(self._job_dir) if self._job_dir else None})
        return outcome

    def convert_open(self, path: str, reveal: bool = False) -> bool:
        """Open (or show in Explorer) a file this session converted."""
        p = Path(str(path))
        if p not in set(self._photos.values()) or not p.is_file():
            return False
        if reveal:
            import subprocess
            subprocess.Popen(["explorer.exe", "/select,", str(p)])  # noqa: S603
        else:
            os.startfile(p)  # noqa: S606
        return True

    # ------------------------------------------------- music & video settings

    def media_analyze(self, url: str, preset_id: str = "") -> dict:
        """What a link holds, before downloading (title, quality, chapters,
        subtitles; a playlist's songs). Nothing is downloaded."""
        from . import mediadl, tools
        if not _is_web_url(url):
            return {"ok": False, "error": "Eso no es un enlace http(s)."}
        ms = mediadl.load_settings()
        preset = next((p for p in ms["presets"] if p["id"] == (preset_id or ms["default_preset"])), ms["presets"][0])
        # The UI test answers from a saved yt-dlp -J instead of the network (never
        # outside a test copy: BAKIN_HOME must be set too).
        fake = os.environ.get("BAKIN_FAKE_ANALYZE")
        if fake and os.environ.get("BAKIN_HOME"):
            try:
                info = mediadl.summarize(json.loads(Path(fake).read_text(encoding="utf-8")), str(url).strip())
            except (OSError, ValueError) as exc:
                return {"ok": False, "error": f"análisis de prueba: {exc}"}
            info["suggested"] = mediadl.suggest_preset(ms, str(url), info.get("detected") or {})
            return {"ok": True, **info}
        if tools.find("yt-dlp") is None:
            return {"ok": False, "field": "tools", "missing": ["yt-dlp"], "error": "Falta yt-dlp: instálalo desde «Herramientas»."}
        try:
            info = mediadl.analyze(str(url).strip(), preset)
        except mediadl.MediaError as exc:
            return {"ok": False, "error": str(exc)}
        except ValueError:
            return {"ok": False, "error": "yt-dlp devolvió datos que no se pudieron leer."}
        info["suggested"] = mediadl.suggest_preset(ms, str(url), info.get("detected") or {})
        if info["type"] == "playlist":
            done = mediadl.archived_ids(mediadl.destination(preset, _library_root(None)))
            for e in info["entries"]:
                e["have"] = e.get("id") in done
        else:
            info["thumbnail"] = _remote_thumb(info.get("thumbnail"))
        return {"ok": True, **info}

    def media_set_site_pref(self, url: str, preset_id: str = "") -> dict:
        """The user corrected the preset for a link: links from that site start
        with it from now on (an empty preset forgets the site)."""
        from . import mediadl
        site = mediadl.site_of(url)
        if not re.fullmatch(r"[a-z0-9.-]{1,100}", site):
            return {"ok": False, "error": "Ese enlace no tiene un sitio."}
        ms = mediadl.load_settings()
        prefs = dict(ms["site_prefs"])
        if preset_id:
            if preset_id not in {p["id"] for p in ms["presets"]}:
                return {"ok": False, "error": "Ese preset ya no existe."}
            prefs[site] = preset_id
        else:
            prefs.pop(site, None)
        mediadl.save_settings({**ms, "site_prefs": prefs})
        return {"ok": True, "site": site, "preset": preset_id or None}

    def media_settings(self) -> dict:
        from . import mediadl, tools
        ms = mediadl.load_settings()
        return {**ms, "tools": tools.status(), "browsers": list(mediadl.BROWSERS),
                "liked_url": mediadl.LIKED_MUSIC_URL,
                "templates": mediadl.TEMPLATES, "sponsorblock": mediadl.SPONSORBLOCK,
                "root": str(_library_root(None))}

    def media_save_preset(self, preset: dict) -> dict:
        from . import mediadl
        p = mediadl.clean_preset(preset or {})
        why = mediadl.check_folder(p, _library_root(None))
        if why:
            return {"ok": False, "error": why}
        ms = mediadl.load_settings()
        presets = [x for x in ms["presets"] if x["id"] != p["id"]]
        idx = next((i for i, x in enumerate(ms["presets"]) if x["id"] == p["id"]), len(presets))
        presets.insert(idx, p)
        mediadl.save_settings({**ms, "presets": presets})
        return {"ok": True, "preset": p}

    def media_restore_builtins(self) -> dict:
        from . import mediadl
        return {"ok": True, "restored": mediadl.restore_builtins()}

    def media_delete_preset(self, preset_id: str) -> dict:
        from . import mediadl
        ms = mediadl.load_settings()
        presets = [x for x in ms["presets"] if x["id"] != preset_id]
        if not presets:
            return {"ok": False, "error": "Tiene que quedar al menos un preset."}
        mediadl.save_settings({**ms, "presets": presets})
        return {"ok": True}

    def media_set_option(self, key: str, value) -> dict:
        from . import mediadl
        ms = mediadl.load_settings()
        if key == "default_preset" and value in {p["id"] for p in ms["presets"]}:
            ms["default_preset"] = value
        elif key == "daily_limit":
            try:
                ms["daily_limit"] = max(0, min(100000, int(value)))
            except (TypeError, ValueError):
                return {"ok": False, "error": "número inválido"}
        elif key in ("verbose", "auto_update"):
            ms[key] = bool(value)
        else:
            return {"ok": False, "error": "opción inválida"}
        mediadl.save_settings(ms)
        return {"ok": True}

    def media_import_presets(self, path: str) -> dict:
        from . import mediadl
        try:
            found = mediadl.import_script_presets(Path(str(path)))
        except (OSError, ValueError, AttributeError, TypeError) as exc:
            return {"ok": False, "error": f"No se pudo leer ese archivo de presets ({exc.__class__.__name__})."}
        ms = mediadl.load_settings()
        names = {p["name"] for p in ms["presets"]}
        for p in found:
            if p["name"] in names:
                p["name"] += " (script)"
        mediadl.save_settings({**ms, "presets": ms["presets"] + found})
        return {"ok": True, "imported": len(found)}

    def media_scan_folder(self, preset_id: str) -> dict:
        """-ScanOnly: register files already in a preset's folder in its archive."""
        from . import mediadl
        ms = mediadl.load_settings()
        preset = next((p for p in ms["presets"] if p["id"] == preset_id), None)
        if preset is None:
            return {"ok": False, "error": "Ese preset ya no existe."}
        folder = mediadl.destination(preset, _library_root(None))
        res = mediadl.scan_existing(folder)
        return {"ok": True, "folder": str(folder), **res}

    def media_redownload(self, preset_id: str, ids: list) -> dict:
        """Invoke-OverrideLeftoverMp4: old .mp4 to the Recycle Bin, ids out of the
        archive, then each one queued again (retrying even single failures)."""
        from . import library, mediadl
        ms = mediadl.load_settings()
        preset = next((p for p in ms["presets"] if p["id"] == preset_id), None)
        if preset is None:
            return {"ok": False, "error": "Ese preset ya no existe."}
        folder = mediadl.destination(preset, _library_root(None))
        wanted = {str(i) for i in ids or []}
        left = [x for x in mediadl.leftover_videos(folder) if x["id"] in wanted]
        for x in left:
            try:
                library.send_to_recycle_bin(Path(x["path"]))
            except library.RecycleError as exc:
                return {"ok": False, "error": f"No se pudo mandar {x['name']} a la Papelera: {exc}"}
        mediadl.forget_in_archive(folder, [x["id"] for x in left])
        queued = 0
        with self._lock:
            for x in left:
                r = self._start_media(f"https://www.youtube.com/watch?v={x['id']}", {"preset": preset_id},
                                      retry_on_partial=True, title=f"Re-descarga {x['id']} · {preset['name']}")
                queued += bool(r.get("ok"))
        self._push_queue()
        self._lib_invalidate()
        return {"ok": True, "queued": queued}

    # ------------------------------------------------------------- tools

    def tools_install(self, names: list | None = None) -> dict:
        """Download/update tools in the background; progress as events."""
        from . import tools
        names = [n for n in (names or list(tools.TOOLS)) if n in tools.TOOLS]
        with self._lock:
            if self._tools_busy:
                return {"ok": False, "error": "Ya se están instalando herramientas."}
            if self._current and self._current.get("kind") == "media":
                return {"ok": False, "error": "Espera a que termine la descarga de música/video."}
            self._tools_busy = True
        threading.Thread(target=self._install_tools, args=(names,), daemon=True, name="bakin-tools").start()
        return {"ok": True}

    def _install_tools(self, names: list[str]) -> None:
        from . import tools
        done, failed = [], []
        try:
            for n in names:
                self._push({"kind": "tools_progress", "name": n, "done": 0, "total": None})
                try:
                    v = tools.install(n, lambda d, t, n=n: self._push(
                        {"kind": "tools_progress", "name": n, "done": d, "total": t}))
                    done.append(f"{n} {v}")
                except Exception as exc:  # network, checksum mismatch, disk
                    failed.append(f"{n}: {exc}")
        finally:
            with self._lock:
                self._tools_busy = False
        self._push({"kind": "tools_done", "ok": not failed, "installed": done, "failed": failed,
                    "tools": tools.status()})

    def tools_check(self) -> dict:
        """Which managed tools have a newer release."""
        from . import tools
        out = {}
        for n in tools.TOOLS:
            try:
                out[n] = tools.update_available(n)
                tools.mark_checked(n)
            except Exception:
                out[n] = None
        return {"updates": out, "tools": tools.status()}

    def media_maintenance(self, force: bool = False) -> dict:
        """Keep yt-dlp current (sites change often; it is what fixes YouTube when
        it starts pushing back), quietly: when the window opens, once a day from
        the scheduler, and right after YouTube stops a download (`force`)."""
        from . import mediadl, tools
        if not mediadl.load_settings()["auto_update"] or not tools.managed_path("yt-dlp").is_file():
            return {"ok": True, "started": False}
        if getattr(self, "_ytdlp_checking", False):
            return {"ok": True, "started": False}
        self._ytdlp_checking = True
        self._ytdlp_checked_at = __import__("time").time()

        def work():
            try:
                if tools.update_available("yt-dlp"):
                    with self._lock:
                        if self._tools_busy or (self._current and self._current.get("kind") == "media"):
                            return
                        self._tools_busy = True
                    try:
                        v = tools.install("yt-dlp")
                    finally:
                        with self._lock:
                            self._tools_busy = False
                    self._push({"kind": "info", "message": f"yt-dlp se actualizó a {v}."})
                tools.mark_checked("yt-dlp")
            except Exception as exc:
                log.info("yt-dlp update check failed: %s", exc.__class__.__name__)
            finally:
                self._ytdlp_checking = False
        threading.Thread(target=work, daemon=True, name="bakin-ytdlp-update").start()
        return {"ok": True, "started": True}
        return {"ok": True, "started": True}
