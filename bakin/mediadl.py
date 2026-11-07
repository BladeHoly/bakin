"""Music and video downloads through yt-dlp (everything yt-dlp supports).

A port of the user's YT-Download.ps1 (v3.3) into Bakin:
* presets (audio format/quality or video, cookies, SponsorBlock, delays,
  file-name template, rate limit, playlist range);
* a download archive per destination folder (archive.txt, the same file the
  script used, so existing folders keep working) plus "register what I
  already have" (ffprobe tags or the [id] in the file name);
* title/uploader capped so paths stay under Windows' 260 characters;
* retries with backoff, partial playlist errors accepted;
* .mp4 files left behind in audio mode (low-quality fallbacks) detected and
  offered for a forced re-download.

Cookies are optional: off (default), only when a download needs a login, or
always; from a browser profile (read by yt-dlp when it runs) or from a
cookies.txt the user picks, or cookies imported into Bakin's vault (encrypted with Windows'
DPAPI; see vault.py). Bakin never logs or shows them, and never keeps them in plain text.
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import tools
from .paths import app_dir, is_inside

CREATE_NO_WINDOW = 0x08000000
ARCHIVE = "archive.txt"
MARKER = ".bakin-media.json"     # marks a media folder as a library collection
AUDIO_FORMATS = ("mp3", "m4a", "opus", "flac", "ogg", "wav", "best")
AUDIO_QUALITIES = ("0", "320", "192", "128")
BROWSERS = ("brave", "firefox", "chrome", "edge", "chromium", "opera", "vivaldi", "whale")
AUDIO_EXT = {".mp3", ".m4a", ".opus", ".flac", ".ogg", ".wav", ".aac"}
TEMPLATES = {
    "artist": "%(uploader)s - %(title)s [%(id)s].%(ext)s",
    "index": "%(playlist_index)02d - %(title)s [%(id)s].%(ext)s",
    "title": "%(title)s [%(id)s].%(ext)s",
    "date": "%(upload_date>%Y%m%d)s - %(title)s [%(id)s].%(ext)s",
    "artist_folder": "%(uploader)s\\%(title)s [%(id)s].%(ext)s",
}
SPONSORBLOCK = {"off": "", "conservative": "sponsor", "moderate": "sponsor,selfpromo",
                "aggressive": "sponsor,selfpromo,intro,outro"}

AUTH_ERRORS = re.compile(
    r"sign in to confirm|confirm your age|age[- ]restricted|inappropriate for some users|"
    r"members[- ]only|join this channel|private video|login required|requires authentication|"
    r"only available for registered users|use --cookies|playlist type is unviewable|"
    r"the playlist does not exist|this playlist is private", re.I)
# Cookies that no longer log in: yt-dlp says so when YouTube rotated them, or
# a song that needs the account still asks for it with the cookies in use.
COOKIES_EXPIRED = re.compile(r"cookies are no longer valid|rotated in the browser", re.I)
ACCOUNT_NEEDED = re.compile(r"confirm your age|age[- ]restricted|inappropriate for some users|login required|"
                            r"requires authentication|use --cookies", re.I)
# YouTube pushing back on volume: stop asking (the queue pauses) instead of
# failing every remaining item.
BLOCKED_ERRORS = re.compile(r"not a bot|HTTP Error 429|too many requests|rate.limit", re.I)
# what goes wrong most and yt-dlp says in English (PLAN 0.23.4)
DISK_FULL_ERRORS = re.compile(r"No space left on device|Errno 28|not enough space on the disk|WinError 112", re.I)
OFFLINE_ERRORS = re.compile(r"getaddrinfo failed|Failed to resolve|Name or service not known|name resolution|"
                            r"Network is unreachable|WinError 1006[01]|WinError 11001|Connection refused|"
                            r"Read timed out|timed out", re.I)
GONE_ERRORS = re.compile(r"Private video|Video unavailable|video is not available|removed by the uploader|"
                         r"account associated with this video has been terminated|This video has been removed", re.I)
COOKIE_DB_ERRORS = re.compile(
    r"could not copy .*cookie database|failed to decrypt with dpapi|cookie database|"
    r"app.bound encryption|failed to load cookies", re.I)


# ----------------------------------------------------------------- presets

LIKED_MUSIC_URL = "https://music.youtube.com/playlist?list=LM"


def builtin_presets(music_folder: str = "Música") -> list[dict]:
    """The presets Bakin ships with. Stable ids: added once per install,
    edits are kept, and a deleted one is not brought back automatically."""
    music = dict(folder=music_folder, mode="audio", audio_format="opus", audio_quality="0",
                 cookies_mode="auto", builtin=True)
    return [
        new_preset("Música", id="bimusic", **music,
                   description="El audio original de YouTube en OPUS, sin recodificar. Cookies solo si el sitio las pide."),
        new_preset("Me gusta de YouTube Music", id="biliked", **{**music, "cookies_mode": "always"},
                   description="Tu lista «Me gusta» (privada): usa cookies siempre. Misma carpeta y registro "
                               "que «Música», así nada se baja dos veces. Cierra Brave antes de empezar."),
        new_preset("Playlist o álbum en orden", id="biplaylist", **music,
                   template="%(playlist_title)s\\%(playlist_index)03d - %(title)s [%(id)s].%(ext)s",
                   description="Una subcarpeta por playlist, con los temas numerados en su orden."),
        new_preset("MP3 compatible", id="bimp3", folder=music_folder, mode="audio", audio_format="mp3",
                   audio_quality="0", cookies_mode="auto", builtin=True,
                   description="MP3 a la máxima calidad variable, para el auto o equipos viejos."),
        new_preset("Video", id="bivideo", folder="Videos", mode="video", template=TEMPLATES["date"],
                   cookies_mode="auto", builtin=True,
                   description="MP4 en la máxima resolución disponible."),
        new_preset("Video para compartir", id="bishare", folder="Videos", mode="video", video_max_height=1080,
                   template=TEMPLATES["date"], cookies_mode="auto", builtin=True,
                   description="MP4 hasta 1080p: más liviano, para mandar o ver en el celular."),
    ]


def default_presets() -> list[dict]:
    return builtin_presets()


def new_preset(name: str, **kw) -> dict:
    p = {
        "id": uuid.uuid4().hex[:10], "name": name, "description": "",
        "folder": "Música",                       # relative to the download folder, or absolute
        "mode": "audio",                          # audio | video
        "audio_format": "mp3", "audio_quality": "0",
        "video_max_height": 0,                    # 0 = best available
        "template": TEMPLATES["artist"],
        "embed_thumbnail": True, "embed_metadata": True,
        "sponsorblock": "",                       # yt-dlp categories, "" = off
        "sleep_min": 4, "sleep_max": 9, "playlist_delay": 20,
        "rate_limit": "", "playlist_items": "",
        "archive": True, "retries": 5,
        "cookies_mode": "off",                    # off | auto | always
        "cookies_source": "browser",              # browser | file | vault (imported into Bakin, encrypted)
        "cookies_browser": "brave", "cookies_profile": "", "cookies_file": "",
        "builtin": False,
        "subtitles": "",                          # video: languages to embed, e.g. "es.*,en.*"; "" = none
        # per-download extras (set by the download dialog, never saved in a preset)
        "section": "", "split_chapters": False,
    }
    p.update(kw)
    return p


def clean_preset(p: dict) -> dict:
    """Validate a preset coming from the UI or a file."""
    base = new_preset(str(p.get("name") or "Preset")[:60])
    out = {**base, **{k: p[k] for k in base if k in p}}
    out["id"] = re.sub(r"[^a-z0-9]", "", str(out["id"]).lower())[:16] or base["id"]
    out["mode"] = out["mode"] if out["mode"] in ("audio", "video") else "audio"
    out["audio_format"] = out["audio_format"] if out["audio_format"] in AUDIO_FORMATS else "mp3"
    out["audio_quality"] = str(out["audio_quality"]) if str(out["audio_quality"]) in AUDIO_QUALITIES else "0"
    out["cookies_mode"] = out["cookies_mode"] if out["cookies_mode"] in ("off", "auto", "always") else "off"
    out["cookies_source"] = out["cookies_source"] if out["cookies_source"] in ("browser", "file", "vault") else "browser"
    out["cookies_browser"] = out["cookies_browser"] if out["cookies_browser"] in BROWSERS else "brave"
    out["cookies_profile"] = re.sub(r"[\r\n:]", "", str(out["cookies_profile"]))[:80]
    for k, lo, hi in (("sleep_min", 0, 600), ("sleep_max", 0, 600), ("playlist_delay", 0, 3600),
                      ("retries", 1, 20), ("video_max_height", 0, 8640)):
        try:
            out[k] = max(lo, min(hi, int(out[k])))
        except (TypeError, ValueError):
            out[k] = base[k]
    out["sleep_max"] = max(out["sleep_max"], out["sleep_min"])
    out["sponsorblock"] = ",".join(c for c in re.split(r"[\s,]+", str(out["sponsorblock"]).lower())
                                   if re.fullmatch(r"[a-z_]+", c))
    out["rate_limit"] = str(out["rate_limit"]).strip() if re.fullmatch(r"\s*(\d+(\.\d+)?[KMG]?)?\s*", str(out["rate_limit"]), re.I) else ""
    out["playlist_items"] = str(out["playlist_items"]).strip() if re.fullmatch(r"[\d:,\-\s]*", str(out["playlist_items"])) else ""
    out["subtitles"] = ",".join(x for x in re.split(r"[\s,]+", str(out["subtitles"]))
                                if re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,4})?(\.\*)?|all", x))
    out["section"] = parse_section(str(out["section"]))
    out["split_chapters"] = bool(out["split_chapters"])
    out["template"] = str(out["template"]).strip() or TEMPLATES["artist"]
    if "%(ext)s" not in out["template"]:
        out["template"] += ".%(ext)s"
    out["folder"] = str(out["folder"]).strip() or "Música"
    for k in ("embed_thumbnail", "embed_metadata", "archive", "builtin"):
        out[k] = bool(out[k])
    return out


def _settings_path() -> Path:
    return app_dir() / "media.json"


def load_settings() -> dict:
    try:
        data = json.loads(_settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    presets = [clean_preset(p) for p in data.get("presets") or []]
    first_run = not presets
    seen = [str(x) for x in data.get("builtins_seen") or []]
    changed = False
    if first_run:
        presets = default_presets()
        seen = [p["id"] for p in presets]
        changed = True
    else:
        # Presets added to Bakin after this install began: add them once.
        have = {p["id"] for p in presets}
        for b in builtin_presets(_music_folder(presets, data.get("default_preset"))):
            if b["id"] not in seen:
                seen.append(b["id"])
                changed = True
                if b["id"] not in have:
                    presets.append(b)
    default = data.get("default_preset")
    if default not in {p["id"] for p in presets}:
        default = presets[0]["id"]
    hours = data.get("hours") if isinstance(data.get("hours"), dict) else {}
    out = {"presets": presets, "default_preset": default, "builtins_seen": seen,
           "daily_limit": max(0, int(data.get("daily_limit", 0) or 0)),
           "hours": {"enabled": bool(hours.get("enabled")),
                     "from": max(0, min(23, int(hours.get("from", 1) or 0))),
                     "to": max(0, min(23, int(hours.get("to", 8) or 0)))},
           "downloads_today": data.get("downloads_today") or {},
           "site_prefs": _clean_site_prefs(data.get("site_prefs"), {p["id"] for p in presets}),
           "verbose": bool(data.get("verbose", False)),
           "auto_update": bool(data.get("auto_update", True)),
           # the plain cookies.txt imported into the vault, until it's deleted (a path, no secret)
           "cookies_imported_from": str(data.get("cookies_imported_from") or "")}
    if changed:
        save_settings(out)  # ids stay stable from now on
    return out


def _clean_site_prefs(raw, preset_ids: set) -> dict:
    """{site: preset id} the user chose; sites and presets that no longer make sense go."""
    if not isinstance(raw, dict):
        return {}
    return {str(k)[:100]: str(v) for k, v in list(raw.items())[:500]
            if re.fullmatch(r"[a-z0-9.-]{1,100}", str(k)) and str(v) in preset_ids}


def site_of(url: str) -> str:
    """The site a link is from, as preferences are kept: «youtube.com»,
    «music.youtube.com», «soundcloud.com» (www. and m. dropped)."""
    import urllib.parse
    host = (urllib.parse.urlparse(str(url or "").strip()).hostname or "").lower()
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host


# ------------------------------------------------------------- audio or video
# A friend's request: tell music from video by itself. yt-dlp's -J says it:
# a site of music (by its extractor or its address), only audio formats, or
# YouTube marking it as a song (YouTube Music, «Music» category, track and
# artist). Anything else with video formats is a video.
AUDIO_EXTRACTORS = {"Soundcloud", "SoundcloudSet", "SoundcloudPlaylist", "SoundcloudUser", "Bandcamp", "BandcampAlbum",
                    "BandcampWeekly", "Audiomack", "AudiomackAlbum", "Mixcloud", "MixcloudPlaylist", "MixcloudUser",
                    "YoutubeMusicSearchURL"}
AUDIO_SITES = ("soundcloud.com", "bandcamp.com", "audiomack.com", "mixcloud.com", "music.youtube.com")


def _audio_site(site: str) -> bool:
    return any(site == s or site.endswith("." + s) for s in AUDIO_SITES)


def classify_media(info: dict, url: str) -> dict:
    """{"kind": "audio" | "video" | "unknown", "why": short reason} for what
    yt-dlp -J --flat-playlist returned about a link."""
    # the pasted link and the page yt-dlp settled on (music.youtube.com comes back as youtube.com)
    sites = [x for x in (site_of(url), site_of(info.get("webpage_url") or "")) if x]
    key = str(info.get("extractor_key") or info.get("ie_key") or "")
    audio_site = next((x for x in sites if _audio_site(x)), None)
    if key in AUDIO_EXTRACTORS or audio_site:
        return {"kind": "audio", "why": f"{audio_site or (sites[0] if sites else 'el sitio')} es de música"}
    entries = info.get("entries")
    if entries is not None:
        keys = {str(e.get("ie_key") or "") for e in entries if isinstance(e, dict)}
        if keys and keys <= AUDIO_EXTRACTORS:
            return {"kind": "audio", "why": "la lista es de un sitio de música"}
        return {"kind": "unknown", "why": "una lista puede tener música o videos"}
    formats = [f for f in info.get("formats") or [] if isinstance(f, dict)]
    if formats and all((f.get("vcodec") or "none") == "none" for f in formats):
        return {"kind": "audio", "why": "solo tiene audio"}
    if info.get("track") and (info.get("artist") or info.get("artists")):
        return {"kind": "audio", "why": "está marcado como canción"}
    if "Music" in (info.get("categories") or []):
        return {"kind": "audio", "why": "está en la categoría Música"}
    if formats:
        return {"kind": "video", "why": "tiene video"}
    return {"kind": "unknown", "why": "no se pudo saber"}


def suggest_preset(settings: dict, url: str, detected: dict) -> dict:
    """The preset a link should start with: the one the user chose for its
    site, else the default music or video preset for what was detected."""
    presets = settings["presets"]
    ids = {p["id"] for p in presets}
    site = site_of(url)
    chosen = settings.get("site_prefs", {}).get(site)
    if chosen in ids:
        return {"preset": chosen, "why": f"lo elegiste para {site}", "site": site}
    kind = detected.get("kind")
    mode = {"audio": "audio", "video": "video"}.get(kind)
    if not mode:
        return {"preset": None, "why": "", "site": site}
    default = next((p for p in presets if p["id"] == settings["default_preset"] and p["mode"] == mode), None)
    pick = default or next((p for p in presets if p["mode"] == mode and p["id"] in ("bimusic", "bivideo")), None) \
        or next((p for p in presets if p["mode"] == mode), None)
    return {"preset": pick["id"] if pick else None, "why": detected.get("why", ""), "site": site}


def _music_folder(presets: list[dict], default_id) -> str:
    """Where this user keeps music: the folder of their default (or first) audio preset."""
    audio = [p for p in presets if p["mode"] == "audio"]
    pick = next((p for p in audio if p["id"] == default_id), audio[0] if audio else None)
    return pick["folder"] if pick else "Música"


def restore_builtins() -> int:
    """Put back the shipped presets the user deleted (edited ones are kept)."""
    s = load_settings()
    have = {p["id"] for p in s["presets"]}
    missing = [b for b in builtin_presets(_music_folder(s["presets"], s["default_preset"])) if b["id"] not in have]
    if missing:
        save_settings({**s, "presets": s["presets"] + missing})
    return len(missing)


def save_settings(s: dict) -> None:
    _settings_path().parent.mkdir(parents=True, exist_ok=True)
    from . import migrate  # the version stays, so the next start has nothing to migrate
    _settings_path().write_text(json.dumps(migrate.stamp("media.json", s), ensure_ascii=False, indent=1), encoding="utf-8")


def import_script_presets(path: Path) -> list[dict]:
    """Presets from YT-Download.ps1 (yt-dlp-presets.json) or its last config (yt-dlp-config.json)."""
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    entries = {"Configuración del script": {"Config": data}} if "Format" in data else data
    out = []
    for name, entry in entries.items():
        c = (entry or {}).get("Config") or {}
        extra = c.get("ExtraOpts") or {}
        delays = c.get("Delays") or {}
        cookies = str(c.get("Cookies") or "none")
        sb = c.get("SponsorBlock") or ""
        if isinstance(sb, bool):
            sb = SPONSORBLOCK["aggressive"] if sb else ""
        p = new_preset(
            str(name)[:60], description=str((entry or {}).get("Description") or ""),
            folder=str(c.get("OutputDir") or "Música"),
            mode="video" if extra.get("DownloadVideo") else "audio",
            audio_format=str(c.get("Format") or "mp3").lower(),
            audio_quality=str(c.get("Quality") or "0"),
            template=str(c.get("OutputTemplate") or TEMPLATES["artist"]),
            sponsorblock=sb,
            sleep_min=delays.get("Min", 4), sleep_max=delays.get("Max", 9),
            playlist_delay=delays.get("Playlist", 20),
            rate_limit=str(extra.get("RateLimit") or ""), playlist_items=str(extra.get("PlaylistItems") or ""),
        )
        if cookies.startswith("file:"):
            p.update(cookies_mode="always", cookies_source="file", cookies_file=cookies[5:])
        elif cookies in BROWSERS:
            p.update(cookies_mode="always", cookies_source="browser", cookies_browser=cookies)
        out.append(clean_preset(p))
    return out


# ------------------------------------------------------------- arguments

def _secs(t: str) -> int | None:
    """"1:02:10", "62:10", "3730" or "1h2m10s" -> seconds."""
    t = t.strip().lower()
    if not t:
        return None
    m = re.fullmatch(r"(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?", t)
    if m and any(m.groups()):
        h, mi, se = (int(x or 0) for x in m.groups())
        return h * 3600 + mi * 60 + se
    parts = t.split(":")
    if all(x.isdigit() for x in parts) and 1 <= len(parts) <= 3:
        n = 0
        for x in parts:
            n = n * 60 + int(x)
        return n
    return None


def parse_section(text: str) -> str:
    """A time range "start-end" (either side may be empty) -> "a-b" in seconds, or ""."""
    text = (text or "").strip()
    if not text or "-" not in text:
        return ""
    a, _, b = text.partition("-")
    sa, sb = _secs(a) if a.strip() else 0, _secs(b) if b.strip() else None
    if sa is None or (b.strip() and sb is None) or (sb is not None and sb <= sa):
        return ""
    return f"{sa}-{sb if sb is not None else ''}"


def _clock(n: int) -> str:
    h, rest = divmod(int(n), 3600)
    m, s = divmod(rest, 60)
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"


def destination(preset: dict, root: Path) -> Path:
    folder = Path(preset["folder"]).expanduser()
    return folder if folder.is_absolute() else root / folder


def cap_template(template: str, out_dir: Path) -> str:
    """Set-TemplateLengthCaps from the script: cap free-text fields so the
    whole path (plus yt-dlp's .part/.temp/.webp intermediates) stays under
    260 characters, leaving [id] and the extension intact."""
    avail = 260 - (len(str(out_dir).rstrip("\\/")) + 1) - 40
    inline = re.search(r"%\(uploader\)s(?![\\/])", template)
    folder = re.search(r"%\(uploader\)s[\\/]", template)
    reserve = 20 + (50 if inline else 0)
    if folder:
        avail -= 50
    title_cap = max(40, min(140, avail - reserve))
    out = template.replace("%(title)s", f"%(title).{title_cap}s")
    return out.replace("%(uploader)s", "%(uploader).50s")


# Cookies imported into Bakin (2026-11-07: the user's cookies.txt sat in plain text in Downloads)
# live in the vault, encrypted; yt-dlp gets a plain copy only while it runs, and the cookies it
# renews on the way are kept. One copy shared by the runs that overlap (two lanes, an analysis).
VAULT_COOKIES = "cookies-youtube"
# Browsers whose cookies yt-dlp can't read on Windows (app-bound encryption)
LOCKED_BROWSERS = frozenset({"brave", "chrome", "chromium", "edge", "opera", "vivaldi", "whale"})
_vault_lock = threading.Lock()
_vault_live: dict = {"path": None, "users": 0, "original": b""}


def _vault_enter() -> None:
    from . import vault
    with _vault_lock:
        if _vault_live["users"] == 0:
            data = vault.get(VAULT_COOKIES)
            if data is None:
                raise MediaError("Las cookies guardadas en Bakin no están: impórtalas de nuevo en Configuración › Cuentas.")
            _vault_live.update(path=vault.scratch(data), original=data)
        _vault_live["users"] += 1


def _vault_leave() -> None:
    from . import vault
    with _vault_lock:
        _vault_live["users"] -= 1
        if _vault_live["users"] > 0:
            return
        path, original = _vault_live["path"], _vault_live["original"]
        _vault_live.update(path=None, original=b"")
    try:
        renewed = path.read_bytes()
        if renewed != original and cookies_text_status(renewed.decode("utf-8", "replace"))["youtube_session"]:
            vault.put(VAULT_COOKIES, renewed)
    except OSError:
        pass
    finally:
        vault.wipe(path)


def _uses_vault(preset: dict) -> bool:
    return preset.get("cookies_mode", "off") != "off" and preset.get("cookies_source") == "vault"


def _with_vault_cookies(preset_at: int):
    """Wraps a function that may run yt-dlp with a preset's cookies (`preset_at`: its argument)."""
    def wrap(fn):
        import functools

        @functools.wraps(fn)
        def run(*args, **kw):
            preset = kw.get("preset") if "preset" in kw else args[preset_at]
            if not _uses_vault(preset):
                return fn(*args, **kw)
            _vault_enter()
            try:
                return fn(*args, **kw)
            finally:
                _vault_leave()
        return run
    return wrap


def cookie_args(preset: dict) -> list[str]:
    if preset["cookies_source"] == "vault":
        path = _vault_live["path"]
        if path is None:  # every caller is wrapped in _with_vault_cookies
            raise MediaError("Las cookies guardadas en Bakin no están listas.")
        return ["--cookies", str(path)]
    if preset["cookies_source"] == "file":
        f = Path(preset["cookies_file"]).expanduser()
        if not f.is_file():
            raise MediaError("No se encontró el archivo de cookies del preset.")
        return ["--cookies", str(f)]
    spec = preset["cookies_browser"] + (f":{preset['cookies_profile']}" if preset["cookies_profile"] else "")
    return ["--cookies-from-browser", spec]


def build_args(preset: dict, url: str, out_dir: Path, *, with_cookies: bool) -> list[str]:
    args = ["--newline", "--no-simulate", "--progress", "--no-colors",
            "--progress-template",
            "download:[bakin-p]%(progress.{status,downloaded_bytes,total_bytes,total_bytes_estimate,speed,eta,fragment_index,fragment_count})j",
            "--progress-template", "postprocess:[bakin-pp]%(progress.{status,postprocessor})j",
            "-O", "before_dl:[bakin-s]%(.{id,title,uploader,duration,playlist_index,n_entries,playlist_title,extractor_key,webpage_url})j",
            "-O", "after_move:[bakin-d]%(.{id,title,filepath,extractor_key})j"]
    ffmpeg = tools.find("ffmpeg")
    if ffmpeg:
        args += ["--ffmpeg-location", str(ffmpeg.parent)]
    deno = tools.find("deno")
    if deno:
        args += ["--js-runtimes", f"deno:{deno}"]
    if preset["mode"] == "video":
        h = preset["video_max_height"]
        cap = f"[height<={h}]" if h else ""
        args += ["--format", f"bestvideo{cap}[ext=mp4]+bestaudio[ext=m4a]/bestvideo{cap}+bestaudio/best{cap}",
                 "--merge-output-format", "mp4"]
    else:
        args += ["--extract-audio", "--audio-format", preset["audio_format"],
                 "--audio-quality", preset["audio_quality"]]
    if preset["embed_thumbnail"]:
        args += ["--embed-thumbnail"]
    if preset["embed_metadata"]:
        args += ["--embed-metadata"]
    section = preset.get("section") or ""
    if preset["archive"] and not section:  # a clip must not mark the whole video as downloaded
        args += ["--download-archive", str(out_dir / ARCHIVE)]
    if section:
        a, _, b = section.partition("-")
        args += ["--download-sections", f"*{a}-{b or 'inf'}"]
        if preset["mode"] == "video":
            args += ["--force-keyframes-at-cuts"]  # exact cuts (re-encodes around them)
    if preset.get("split_chapters"):
        args += ["--split-chapters", "-o",
                 "chapter:" + str(out_dir / "%(title).80s" / "%(section_number)03d - %(section_title).80s.%(ext)s")]
    if preset["mode"] == "video" and preset.get("subtitles"):
        args += ["--write-subs", "--write-auto-subs", "--sub-langs", preset["subtitles"], "--embed-subs"]
    template = preset["template"]
    if section:  # the clip gets its own name: "Title [id] (tramo 1h02m10s-1h15m00s)"
        a, _, b = section.partition("-")
        template = template.replace(".%(ext)s", f" (tramo {_clock(int(a))}-{_clock(int(b)) if b else 'fin'}).%(ext)s")
    args += ["--no-overwrites", "--output", str(out_dir / cap_template(template, out_dir))]
    if preset["sleep_max"]:
        args += ["--sleep-interval", str(preset["sleep_min"]), "--max-sleep-interval", str(preset["sleep_max"])]
    args += ["--retries", str(preset["retries"]), "--fragment-retries", str(preset["retries"]),
             "--no-abort-on-error", "--ignore-errors"]
    if with_cookies:
        args += cookie_args(preset)
    if preset["sponsorblock"]:
        args += ["--sponsorblock-remove", preset["sponsorblock"]]
    if preset["rate_limit"]:
        args += ["--limit-rate", preset["rate_limit"]]
    if preset["playlist_items"]:
        args += ["--playlist-items", preset["playlist_items"]]
    if preset.get("_max_downloads"):
        args += ["--max-downloads", str(preset["_max_downloads"])]  # today's remaining budget
    return args + ["--", url]


# ------------------------------------------------------------------ running

class MediaError(Exception):
    pass


@dataclass
class MediaStats:
    urls: int = 0
    done: int = 0
    skipped: int = 0
    failed: int = 0
    bytes: int = 0
    files: list = field(default_factory=list)
    blocked: bool = False
    limit_reached: bool = False
    cookies_bad: bool = False  # the preset's cookies no longer log in
    errors: list = field(default_factory=list)  # why songs failed, readable (the first few)

    def as_dict(self) -> dict:
        return {"urls": self.urls, "done": self.done, "skipped": self.skipped,
                "failed": self.failed, "bytes": self.bytes}


def mark_folder(out_dir: Path, preset: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    marker = out_dir / MARKER
    if not marker.exists():
        marker.write_text(json.dumps({"preset": preset["name"],
                                      "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                                     ensure_ascii=False), encoding="utf-8")


class Runner:
    """One yt-dlp process at a time; lines become events."""

    def __init__(self, emit: Callable[[dict], None], cancel: threading.Event, verbose: bool = False):
        self.emit = emit
        self.cancel = cancel
        self.verbose = verbose
        self.proc: subprocess.Popen | None = None
        self._last_progress = 0.0

    def run(self, args: list[str]) -> tuple[int, dict]:
        exe = tools.find("yt-dlp")
        if exe is None:
            raise MediaError("Falta yt-dlp: instálalo desde «Herramientas».")
        self.proc = subprocess.Popen([str(exe), *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, env=tools.child_env(), creationflags=CREATE_NO_WINDOW)
        seen = {"done": [], "errors": [], "started": [], "n_entries": None, "cookies_expired": False}
        killer = threading.Thread(target=self._watch_cancel, daemon=True)
        killer.start()
        for raw in self.proc.stdout:
            self._line(raw.decode("utf-8", "replace").rstrip("\r\n"), seen)
        code = self.proc.wait()
        return code, seen

    def _watch_cancel(self) -> None:
        while self.proc and self.proc.poll() is None:
            if self.cancel.wait(0.3):
                tools.kill_tree(self.proc.pid)
                return

    def _json(self, line: str, tag: str) -> dict | None:
        try:
            return json.loads(line[len(tag):])
        except ValueError:
            return None

    def _line(self, line: str, seen: dict) -> None:
        if not line.strip():
            return
        if line.startswith("[bakin-p]"):
            p = self._json(line, "[bakin-p]") or {}
            now = time.monotonic()
            if p.get("status") == "downloading" and now - self._last_progress < 0.25:
                return
            self._last_progress = now
            total = p.get("total_bytes") or p.get("total_bytes_estimate")
            self.emit({"kind": "media_progress", "status": p.get("status"),
                       "downloaded": p.get("downloaded_bytes"), "total": total,
                       "speed": p.get("speed"), "eta": p.get("eta")})
        elif line.startswith("[bakin-pp]"):
            p = self._json(line, "[bakin-pp]") or {}
            if p.get("status") == "started":
                self.emit({"kind": "media_post", "step": p.get("postprocessor")})
        elif line.startswith("[bakin-s]"):
            info = self._json(line, "[bakin-s]") or {}
            seen["started"].append(info.get("id"))
            if info.get("n_entries"):
                seen["n_entries"] = info["n_entries"]
            self.emit({"kind": "media_item", **{k: info.get(k) for k in
                       ("id", "title", "uploader", "duration", "playlist_index", "n_entries", "playlist_title",
                        "extractor_key")}})
        elif line.startswith("[bakin-d]"):
            info = self._json(line, "[bakin-d]") or {}
            seen["done"].append(info)
            self.emit({"kind": "media_file", "id": info.get("id"), "title": info.get("title"),
                       "path": info.get("filepath")})
        elif line.startswith("ERROR:"):
            msg = line[6:].strip()
            seen["errors"].append(msg)
            self.emit({"kind": "error", "message": friendly_error(msg)})
        elif self.verbose or line.startswith("WARNING:"):
            if COOKIES_EXPIRED.search(line):
                seen["cookies_expired"] = True
            if self.verbose or not re.search(r"Falling back|has already been|sleeping", line, re.I):
                self.emit({"kind": "media_raw", "message": line})


def _cookies_failed(seen: dict) -> bool:
    """A run made with cookies that still wasn't logged in."""
    return bool(seen.get("cookies_expired")) or any(
        ACCOUNT_NEEDED.search(e) and not BLOCKED_ERRORS.search(e) for e in seen["errors"])


# Cookies that carry a Google/YouTube login (names only: values are never read
# into anything Bakin keeps or shows).
SESSION_COOKIES = {"SID", "__Secure-1PSID", "__Secure-3PSID", "SAPISID", "__Secure-3PAPISID", "LOGIN_INFO"}


def cookies_file_status(path: str) -> dict:
    """What a cookies.txt holds, without its values: whether it exists, has a
    YouTube login, and when that login's cookies expire."""
    out = {"file": str(path), "exists": False, "count": 0, "youtube_session": False, "expires": None,
           "expired": False, "mtime": None}
    p = Path(path).expanduser()
    try:
        st = p.stat()
        if not p.is_file():
            return out
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    return {**out, **cookies_text_status(text), "exists": True, "mtime": int(st.st_mtime)}


def cookies_text_status(text: str) -> dict:
    """The same, for cookies already read (the vault's): never their values."""
    import time as _t
    out = {"count": 0, "youtube_session": False, "expires": None, "expired": False}
    expiries = []
    for line in text.splitlines():
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        out["count"] += 1
        domain, name = parts[0].lower(), parts[5]
        if name in SESSION_COOKIES and (domain.endswith("youtube.com") or domain.endswith("google.com")):
            out["youtube_session"] = True
            try:
                exp = int(float(parts[4]))
            except ValueError:
                exp = 0
            if exp > 0:
                expiries.append(exp)
    if expiries:
        out["expires"] = min(expiries)
        out["expired"] = out["expires"] < _t.time()
    return out


def friendly_error(msg: str) -> str:
    if re.search(r"decrypt with dpapi|app.bound", msg, re.I):
        return ("Las cookies de Brave, Chrome y Edge están cifradas por el propio navegador y yt-dlp no "
                "puede leerlas en Windows. Usa en el preset un archivo cookies.txt (exportado desde una "
                f"ventana privada) o Firefox. ({msg[:120]})")
    if BLOCKED_ERRORS.search(msg):
        return ("YouTube está frenando las descargas desde tu conexión («confirma que no eres un bot» o 429), "
                "por la cantidad de pedidos. Se pasa solo en unas horas; no hace falta usar tu cuenta. "
                f"({msg[:120]})")
    # before «sign in»: a private video says both, and signing in won't fix it
    if GONE_ERRORS.search(msg):
        return f"El video es privado o ya no está disponible. ({msg[:160]})"
    if COOKIE_DB_ERRORS.search(msg):
        return ("No se pudieron leer las cookies del navegador. Si usas Brave u otro navegador "
                "Chromium, ciérralo por completo y reintenta, o usa un archivo cookies.txt. "
                f"({msg[:160]})")
    if AUTH_ERRORS.search(msg):
        return f"El sitio pide iniciar sesión para este contenido (usa cookies en el preset). ({msg[:200]})"
    if DISK_FULL_ERRORS.search(msg):
        return ("No queda espacio en el disco de la carpeta de descargas. Libera espacio o cambia la "
                f"carpeta en Configuración › Descargas; lo terminado se conserva. ({msg[:120]})")
    if OFFLINE_ERRORS.search(msg):
        return ("No hay conexión a internet, o el sitio no responde. Lo terminado se conserva; "
                f"reintenta en un rato. ({msg[:120]})")
    if "HTTP Error 403" in msg:
        return f"El sitio rechazó la descarga (403). Actualiza yt-dlp desde Herramientas y reintenta. ({msg[:160]})"
    return msg[:400]


@_with_vault_cookies(1)
def download(urls: list[str], preset: dict, root: Path, emit: Callable[[dict], None],
             cancel: threading.Event, *, verbose: bool = False,
             retry_on_partial: bool = False, mark: bool = True, max_downloads: int | None = None) -> MediaStats:
    """Invoke-Download for every URL, with the delay between URLs."""
    out_dir = destination(preset, root)
    if mark:
        mark_folder(out_dir, preset)
    else:
        out_dir.mkdir(parents=True, exist_ok=True)
    stats = MediaStats(urls=len(urls))
    runner = Runner(emit, cancel, verbose)
    if max_downloads:
        preset = {**preset, "_max_downloads": max_downloads}
    for i, url in enumerate(urls):
        if cancel.is_set():
            break
        if i:
            wait = preset["playlist_delay"]
            if wait:
                emit({"kind": "wait", "seconds": wait, "message": "pausa entre enlaces"})
                if cancel.wait(wait):
                    break
        emit({"kind": "media_url", "url": url, "index": i + 1, "count": len(urls)})
        use_cookies = preset["cookies_mode"] == "always"
        seen = _run_with_retries(runner, preset, url, out_dir, use_cookies, retry_on_partial, emit, cancel)
        if seen is not None and use_cookies and _cookies_failed(seen):
            stats.cookies_bad = True
        if (seen is not None and use_cookies and not cancel.is_set()
                and any(COOKIE_DB_ERRORS.search(e) for e in seen["errors"])):
            # The browser is open and locks its cookies: public content does
            # not need them, so try once without (private lists will say so).
            emit({"kind": "warn", "message": "No se pudieron leer las cookies (¿el navegador está abierto?): "
                                             "se intenta sin cookies."})
            seen = _run_with_retries(runner, preset, url, out_dir, False, retry_on_partial, emit, cancel)
        if (seen is not None and preset["cookies_mode"] == "auto" and not cancel.is_set()
                and any(AUTH_ERRORS.search(e) for e in seen["errors"])
                # "Not a bot" is YouTube flagging the connection, not a song that
                # needs an account: don't spend the user's account to get past it.
                and not any(BLOCKED_ERRORS.search(e) for e in seen["errors"])):
            emit({"kind": "info", "message": "El sitio pidió iniciar sesión: se reintenta con las cookies del preset."})
            retry = _run_with_retries(runner, preset, url, out_dir, True, retry_on_partial, emit, cancel)
            if retry is not None and _cookies_failed(retry):
                stats.cookies_bad = True
            if retry is not None:
                seen = {**retry, "errors": [e for e in seen["errors"] if not AUTH_ERRORS.search(e)] + retry["errors"],
                        "done": seen["done"] + retry["done"]}
        if seen is None:
            continue
        for d in seen["done"]:
            p = Path(d.get("filepath") or "")
            if p.is_file():
                stats.bytes += p.stat().st_size
                stats.files.append(str(p))
        stats.done += len(seen["done"])
        if seen.get("limit_reached"):
            stats.limit_reached = True
        stats.failed += len(set(seen["errors"]))
        for e in dict.fromkeys(seen["errors"]):
            if len(stats.errors) < 5:
                stats.errors.append(friendly_error(re.sub(r"^ERROR:\s*", "", e.strip())))
        if any(BLOCKED_ERRORS.search(e) for e in seen["errors"]):
            stats.blocked = True
            break
        # Items in the archive are skipped silently, before they "start".
        started = len(set(seen["started"]))
        if seen["n_entries"]:
            stats.skipped += max(0, seen["n_entries"] - started)
        elif not started and not seen["errors"]:
            stats.skipped += 1
    return stats


def _run_with_retries(runner: Runner, preset: dict, url: str, out_dir: Path, cookies: bool,
                      retry_on_partial: bool, emit, cancel) -> dict | None:
    attempts = preset["retries"]
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            wait = 2 ** attempt
            emit({"kind": "info", "message": f"Reintento {attempt}/{attempts} en {wait} s…"})
            if cancel.wait(wait):
                return None
        try:
            code, seen = runner.run(build_args(preset, url, out_dir, with_cookies=cookies))
        except MediaError:
            raise
        except OSError as exc:
            emit({"kind": "error", "message": f"No se pudo ejecutar yt-dlp: {exc}"})
            continue
        if cancel.is_set():
            return seen
        if code == 0:
            return seen
        if code == 101:  # --max-downloads reached: today's budget is spent
            seen["limit_reached"] = True
            return seen
        if any(COOKIE_DB_ERRORS.search(e) or BLOCKED_ERRORS.search(e) for e in seen["errors"]):
            # Retrying won't unlock the cookie database, and hammering YouTube
            # after "confirm you're not a bot" only makes the block last longer.
            return seen
        if code == 1:
            # With --ignore-errors, 1 means "some items failed" (private,
            # deleted, region-locked). Retry only single forced re-downloads.
            if retry_on_partial and not seen["done"] and attempt < attempts:
                continue
            return seen
        emit({"kind": "warn", "message": f"yt-dlp terminó con el código {code}."})
    return None


@_with_vault_cookies(1)
def analyze(url: str, preset: dict, cancel: threading.Event | None = None) -> dict:
    """What a link holds, without downloading (one `yt-dlp -J --flat-playlist`):
    title, length, best quality, chapters, subtitles; for a playlist, its songs.
    Cookies only if the preset always uses them, or the site asks."""
    exe = tools.find("yt-dlp")
    if exe is None:
        raise MediaError("Falta yt-dlp: instálalo desde «Herramientas».")

    def run(with_cookies: bool):
        args = [str(exe), "-J", "--flat-playlist", "--no-warnings", "--yes-playlist"]
        deno = tools.find("deno")
        if deno:
            args += ["--js-runtimes", f"deno:{deno}"]
        if with_cookies:
            args += cookie_args(preset)
        proc = subprocess.Popen(args + ["--", url], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=tools.child_env(), creationflags=CREATE_NO_WINDOW)
        try:
            out, err = proc.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            tools.kill_tree(proc.pid)
            raise MediaError("Analizar el enlace tardó demasiado.") from None
        return proc.returncode, out, err.decode("utf-8", "replace")

    cookies_first = preset["cookies_mode"] == "always" or is_private_list(url)
    code, out, err = run(cookies_first)
    if code != 0 and not cookies_first and preset["cookies_mode"] == "auto" and AUTH_ERRORS.search(err) \
            and not BLOCKED_ERRORS.search(err):
        code, out, err = run(True)
    if code != 0 or not out.strip():
        msg = next((l for l in err.splitlines() if l.startswith("ERROR")), err.strip()[-300:] or "sin datos")
        raise MediaError(friendly_error(msg.replace("ERROR:", "").strip()))
    return summarize(json.loads(out), url)


def summarize(info: dict, url: str) -> dict:
    """What analyze() shows of yt-dlp -J's output (also fed directly by tests)."""
    entries = info.get("entries")
    if entries is not None:
        rows = []
        for i, e in enumerate(entries, 1):
            if not isinstance(e, dict):
                continue
            rows.append({"index": e.get("playlist_index") or i, "id": e.get("id"), "title": e.get("title") or e.get("id"),
                         "duration": e.get("duration"), "uploader": e.get("uploader") or e.get("channel"),
                         "thumb": entry_thumb(e)})
        return {"type": "playlist", "title": info.get("title"), "uploader": info.get("uploader") or info.get("channel"),
                "count": len(rows), "entries": rows[:5000], "url": info.get("webpage_url") or url,
                "detected": classify_media(info, url)}
    formats = info.get("formats") or []
    heights = [f.get("height") or 0 for f in formats if (f.get("vcodec") or "none") != "none"]
    audio = max((f for f in formats if (f.get("acodec") or "none") != "none" and (f.get("vcodec") or "none") == "none"),
                key=lambda f: f.get("abr") or 0, default=None)
    subs = sorted((info.get("subtitles") or {}).keys())
    return {"type": "video", "title": info.get("title"), "uploader": info.get("uploader") or info.get("channel"),
            "duration": info.get("duration"), "thumbnail": info.get("thumbnail"), "id": info.get("id"),
            "max_height": max(heights) if heights else 0,
            "audio": f"{audio.get('acodec', '').split('.')[0]} {round(audio.get('abr') or 0)} kbps" if audio else "",
            "chapters": [{"title": c.get("title"), "start": c.get("start_time"), "end": c.get("end_time")}
                         for c in (info.get("chapters") or [])],
            "subtitles": subs, "auto_subtitles": len(info.get("automatic_captions") or {}),
            "age_limit": info.get("age_limit") or 0, "is_live": bool(info.get("is_live")),
            "url": info.get("webpage_url") or url, "detected": classify_media(info, url)}


def is_private_list(url: str) -> bool:
    """YouTube's private auto-playlists: Liked Music (LM) and Liked videos (LL)."""
    return bool(re.search(r"[?&]list=(LM|LL)(&|$)", url or ""))


@_with_vault_cookies(1)
def list_ids(url: str, preset: dict, cancel: threading.Event) -> list[str]:
    """The video ids of a playlist, without downloading (--flat-playlist),
    using the preset's cookies (needed for private lists)."""
    exe = tools.find("yt-dlp")
    if exe is None:
        raise MediaError("Falta yt-dlp: instálalo desde «Herramientas».")
    args = [str(exe), "--flat-playlist", "--print", "%(id)s", "--no-warnings"]
    if preset["cookies_mode"] != "off":
        args += cookie_args(preset)
    deno = tools.find("deno")
    if deno:
        args += ["--js-runtimes", f"deno:{deno}"]
    proc = subprocess.Popen(args + ["--", url], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=tools.child_env(), creationflags=CREATE_NO_WINDOW)
    try:
        out, err = proc.communicate(timeout=1200)
    except subprocess.TimeoutExpired:
        tools.kill_tree(proc.pid)
        raise MediaError("Leer la lista tardó demasiado.") from None
    ids = [x.strip() for x in out.decode("utf-8", "replace").splitlines() if re.fullmatch(r"[A-Za-z0-9_-]{11}", x.strip())]
    if not ids:
        msg = next((l for l in err.decode("utf-8", "replace").splitlines() if l.startswith("ERROR")), "sin canciones")
        raise MediaError(f"No se pudo leer la lista: {friendly_error(msg.replace('ERROR:', '').strip())}")
    return list(dict.fromkeys(ids))


def entry_thumb(e: dict) -> str:
    """A small thumbnail URL for a playlist entry: YouTube's own image CDN
    only (the window's content policy allows nothing else)."""
    vid = str(e.get("id") or "")
    if (e.get("ie_key") or "").lower() in ("youtube", "") and re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
        return f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
    return ""


@_with_vault_cookies(1)
def list_entries(url: str, preset: dict) -> list[tuple[str, str]]:
    """(extractor, id) of everything a channel/playlist holds right now,
    without downloading: what "only new from now on" marks as seen."""
    exe = tools.find("yt-dlp")
    if exe is None:
        raise MediaError("Falta yt-dlp: instálalo desde «Herramientas».")
    args = [str(exe), "--flat-playlist", "--print", "%(ie_key)s %(id)s", "--no-warnings"]
    if preset["cookies_mode"] == "always" or is_private_list(url):
        args += cookie_args(preset)
    proc = subprocess.Popen(args + ["--", url], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=tools.child_env(), creationflags=CREATE_NO_WINDOW)
    try:
        out, err = proc.communicate(timeout=1200)
    except subprocess.TimeoutExpired:
        tools.kill_tree(proc.pid)
        raise MediaError("Leer la lista tardó demasiado.") from None
    rows = []
    for line in out.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] != "NA" and re.fullmatch(r"[\w-]+", parts[1]):
            rows.append((parts[0].lower(), parts[1]))
    if proc.returncode != 0 and not rows:
        msg = next((l for l in err.decode("utf-8", "replace").splitlines() if l.startswith("ERROR")), "sin datos")
        raise MediaError(friendly_error(msg.replace("ERROR:", "").strip()))
    return rows


def mark_seen(folder: Path, entries: list[tuple[str, str]]) -> int:
    """Add entries to the folder's archive.txt (yt-dlp's own format), so they
    count as downloaded and only what comes later is fetched."""
    folder.mkdir(parents=True, exist_ok=True)
    have = archived_ids(folder)
    new = [f"{ie} {vid}" for ie, vid in entries if vid not in have]
    if new:
        with open(folder / ARCHIVE, "a", encoding="utf-8") as fp:
            fp.write("".join(x + "\n" for x in new))
    return len(new)


def archived_ids(folder: Path) -> set[str]:
    try:
        return {ln.split()[-1] for ln in (folder / ARCHIVE).read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()}
    except OSError:
        return set()


# ------------------------------------------------- scan / leftovers / archive

ID_IN_NAME = re.compile(r"\[([A-Za-z0-9_-]{11})\]")


def _id_from_tags(path: Path) -> str | None:
    probe = tools.find("ffmpeg", "ffprobe.exe")
    if probe is None:
        return None
    out = tools.run_quick([str(probe), "-v", "quiet", "-print_format", "json", "-show_entries",
                           "format_tags", str(path)], timeout=30)
    try:
        tags = (json.loads(out or "{}").get("format") or {}).get("tags") or {}
    except ValueError:
        return None
    for k, v in tags.items():
        if k.lower() in ("purl", "purchase_url", "comment"):
            m = re.search(r"(?:v=|youtu\.be/|embed/)([A-Za-z0-9_-]{11})", str(v))
            if m:
                return m.group(1)
    return None


def scan_existing(folder: Path, progress: Callable[[int, int], None] = lambda i, n: None) -> dict:
    """Invoke-ScanExistingFiles: put YouTube ids of audio files already in a
    folder into its archive.txt so they are not downloaded again."""
    archive = folder / ARCHIVE
    known = set()
    if archive.is_file():
        known = {line.strip().lower() for line in archive.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()}
    files = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXT) if folder.is_dir() else []
    new, already, no_id = [], 0, []
    for i, f in enumerate(files, 1):
        progress(i, len(files))
        vid = _id_from_tags(f) or (m.group(1) if (m := ID_IN_NAME.search(f.name)) else None)
        if not vid:
            no_id.append(f.name)
            continue
        entry = f"youtube {vid}"
        if entry.lower() in known:
            already += 1
        else:
            new.append(entry)
            known.add(entry.lower())
    if new:
        with open(archive, "a", encoding="utf-8") as fp:
            fp.write("".join(e + "\n" for e in new))
    return {"files": len(files), "registered": len(new), "already": already, "no_id": no_id}


def leftover_videos(folder: Path) -> list[dict]:
    """Get-LeftoverMp4Ids: .mp4 files with an [id] in an audio folder."""
    out = []
    if folder.is_dir():
        for p in folder.rglob("*.mp4"):
            m = re.search(r"\[([A-Za-z0-9_-]{6,})\]$", p.stem)
            if m:
                out.append({"id": m.group(1), "path": str(p), "name": p.name})
    return out


def forget_in_archive(folder: Path, ids: list[str]) -> int:
    archive = folder / ARCHIVE
    if not archive.is_file() or not ids:
        return 0
    lines = archive.read_text(encoding="utf-8", errors="replace").splitlines()
    keep = [ln for ln in lines if not any(re.search(rf"\b{re.escape(i)}\b", ln) for i in ids)]
    archive.write_text("".join(ln + "\n" for ln in keep), encoding="utf-8")
    return len(lines) - len(keep)


def is_media_folder(d: Path) -> bool:
    return (d / MARKER).is_file()


def check_folder(preset: dict, root: Path) -> str | None:
    d = destination(preset, root)
    if is_inside(d, app_dir()):
        return "La carpeta del preset no puede estar dentro de los datos privados de Bakin."
    return None
