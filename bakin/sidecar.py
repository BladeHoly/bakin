"""Engine process for the Tauri shell (desktop/): JSON lines over stdin/stdout.

    python -m bakin.sidecar

Protocol (one JSON object per line, UTF-8):
  in : {"id": 7, "method": "start", "params": {"mode": "download", "url": "...", "options": {...}}}
  out: {"id": 7, "result": {...}}  or  {"id": 7, "error": "..."}
  out: {"event": {...}}            progress pushed by the running job
  out: {"ready": true, "version": "x.y.z"} once, at start-up

Only the methods in METHODS can be called; their arguments are validated by
bakin.service. Requests are never logged (they may carry paths and cookie
file locations), and stdout carries nothing but protocol lines: anything else a
library prints goes to stderr. Exits when stdin closes (the window quit).
"""

from __future__ import annotations

import json
import logging
import sys
import threading

from . import __version__, redact
from .paths import log_file
from .service import Service

METHODS = frozenset({
    "get_state", "job_running",
    "start", "cancel", "cancel_current", "remove_queued", "resume_queue", "pause_queue", "set_out_dir", "undo_move", "open_dir", "log_info", "open_logs", "sleep_if_idle", "resume_due", "wallpaper_sources", "wallhaven_account", "wallhaven_account_set", "wallhaven_account_clear", "wallhaven_my_collections", "wallhaven_follow", "wallhaven_collection", "queue_pause", "queue_resume", "library_roots", "library_overview", "library_items", "library_set_favorite", "library_tag", "library_delete", "library_deleted", "library_restore", "library_forget", "library_add_folder", "library_remove_folder", "library_open", "library_reveal",
    "media_settings", "media_analyze", "music_tracks", "music_cover", "music_played", "music_state_get", "music_state_set", "music_overview", "music_groups", "music_playlist_save", "music_playlist_add", "music_playlist_remove", "music_playlist_delete", "music_playlist_export", "music_mirror_set", "music_mirror_off", "music_mirror_sync", "music_mirror_open", "lan_library", "lan_star", "lan_scrobble", "lan_cover", "lan_lyrics", "lan_code", "first_run_done", "cookies_status", "set_hours", "follows_list", "follow_add", "follow_remove", "follow_check_now", "media_save_preset", "media_delete_preset", "media_restore_builtins", "media_set_option",
    "media_import_presets", "media_set_site_pref", "media_scan_folder", "media_redownload", "media_maintenance",
    "tools_install", "tools_check",
    "library_upgrade_audio", "convert_formats", "convert_inspect", "convert_start", "convert_open",
    "wallhaven_search", "wallhaven_download", "wallhaven_open", "music_search_open", "cookies_vault", "cookies_import", "cookies_delete_original", "cookies_forget", "cookies_use_vault", "library_redownload", "music_clean_propose", "music_clean_apply", "music_tags_undo", "music_mb_start", "music_mb_enable", "catalog_search", "catalog_page", "catalog_thumbs", "catalog_download", "spotify_resolve", "spotify_cancel", "read_text_file", "settings_set", "music_loudness_scan", "music_gains", "music_lyrics", "music_lyrics_fetch", "data_dir_info", "data_dir_move", "backup_export", "backup_inspect", "backup_import", "music_mb_cancel", "music_dupes", "music_dupes_resolve", "music_stats",
})

log = logging.getLogger("bakin.sidecar")


class Channel:
    """Serialised protocol output on the real stdout."""

    def __init__(self, stream):
        self._stream = stream
        self._lock = threading.Lock()

    def send(self, obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=False, default=str)
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()


def handle(service: Service, channel: Channel, request: dict) -> None:
    rid = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}
    if method not in METHODS or not isinstance(params, dict):
        channel.send({"id": rid, "error": f"unknown method: {method!r}"})
        return
    try:
        result = getattr(service, method)(**params)
        channel.send({"id": rid, "result": result})
    except TypeError:
        channel.send({"id": rid, "error": f"bad arguments for {method}"})
    except Exception as exc:
        log.error("%s failed:\n%s", method, redact.format_exception_redacted(exc))
        channel.send({"id": rid, "error": redact.redact(f"{exc.__class__.__name__}: {exc}")})


def serve(stdin, stdout) -> None:
    channel = Channel(stdout)
    service = Service(push=lambda payload: channel.send({"event": payload}))
    channel.send({"ready": True, "version": __version__})
    in_flight: list[threading.Thread] = []
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        if not isinstance(request, dict):
            continue
        # Each request on its own thread: a slow one (a Wallhaven search, a
        # library scan) must not hold back progress events or a cancel.
        t = threading.Thread(target=handle, args=(service, channel, request), daemon=True)
        t.start()
        in_flight = [x for x in in_flight if x.is_alive()] + [t]
    service.shutdown()  # the app is closing: stop the job, keep the queue for next time
    # Let requests already being answered finish, so their replies aren't lost.
    for t in in_flight:
        t.join(timeout=5)


def main() -> int:
    stdin = sys.stdin
    stdin.reconfigure(encoding="utf-8")
    proto = sys.stdout
    proto.reconfigure(encoding="utf-8", newline="\n")
    sys.stdout = sys.stderr  # stray prints must never corrupt the protocol
    redact.setup_logging(log_file())
    redact.install_excepthook()
    try:  # older data files into today's shapes, once (PLAN 0.18.2); never fatal
        from . import migrate
        migrate.run()
    except Exception as exc:
        log.warning("migrations skipped: %s", exc.__class__.__name__)
    serve(stdin, proto)
    return 0


if __name__ == "__main__":
    sys.exit(main())
