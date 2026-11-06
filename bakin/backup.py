"""Exportar / importar (PLAN 0.18.1): what the user built in Bakin, in one zip.

The zip holds an explicit allowlist of small files from Bakin's private data
folder: settings, presets, play counts and history, skips, playlists,
loudness, lyrics, follows, the queue, the folders added to the library, and
every collection's favourites / tags / deletions. Never the vault, cookies,
credentials, tools, logs, thumbnails or caches: those are not on the list,
and the few fields that could point at a secret are blanked or dropped.

Importing merges and never deletes: favourites and tags are joined, plays
added up, playlists kept unless the imported copy is newer, and anything
else only added where it's missing here. A copy of the current state is
written to backups/ first.

Layout of the zip:
    manifest.json                 {"bakin_version", "schema", "id", "created", "files",
                                   "collections": {id: path}, "meta": {id: "meta/<x>.json"}}
    ui-settings.json              (keys that could hold a secret dropped)
    media.json                    (presets; cookies_file and the imported-cookies path blanked)
    library-folders.json          {"folders": [...]}
    follows.json  queue.json  wallpapers.json
    music/plays.json  music/plays-log.jsonl  music/skips.json
    music/playlists.json  music/loudness.json  music/lyrics/<sha1>.lrc
    meta/<collection id, sanitized>.json
"""

from __future__ import annotations

import io
import json
import os
import re
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

from . import __version__
from .paths import app_dir

SCHEMA = 1
MANIFEST = "manifest.json"
MAX_ENTRIES = 50_000
MAX_TOTAL = 512 * 1024 * 1024        # uncompressed bytes: a zip bomb stops here
MAX_FILE = 256 * 1024 * 1024

# Plain files copied from app_dir() as they are (after the checks below).
_FIXED = ("ui-settings.json", "media.json", "library-folders.json", "follows.json", "queue.json",
          "wallpapers.json", "music/plays.json", "music/plays-log.jsonl", "music/skips.json",
          "music/playlists.json", "music/loudness.json")
_LYRIC = re.compile(r"music/lyrics/[A-Za-z0-9_-]{1,80}\.lrc")
_META = re.compile(r"meta/[A-Za-z0-9_-][A-Za-z0-9._-]{0,119}\.json")
# Any key whose name says it might carry a secret (ui-settings, follows, the queue).
_SECRETISH = re.compile(r"pass(word|wd)?|secret|token|api_?key|apikey|cookie|session|credential|auth", re.I)


class BackupError(Exception):
    """A message for the user (Spanish)."""


def allowed(name: str) -> bool:
    """Whether a zip entry name is one Bakin writes (and nothing that could escape a folder)."""
    if not name or name.startswith(("/", "\\")) or "\\" in name or ":" in name or "\0" in name:
        return False
    if any(part in ("", ".", "..") for part in name.split("/")):
        return False
    return name == MANIFEST or name in _FIXED or bool(_LYRIC.fullmatch(name) or _META.fullmatch(name))


# ---------------------------------------------------------------- helpers

def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fp:
            fp.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _atomic_json(path: Path, data, indent: int | None = 1) -> None:
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=indent).encode("utf-8"))


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _scrub(obj):
    """Drop every key that could hold a secret, at any depth."""
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items() if not _SECRETISH.search(str(k))}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


def _clean_media(data: dict) -> dict:
    """media.json without paths to cookies files and without today's counters."""
    out = {k: v for k, v in data.items() if k not in ("downloads_today",)}
    out["cookies_imported_from"] = ""
    presets = []
    for p in data.get("presets") or []:
        if isinstance(p, dict):
            p = dict(p)
            if "cookies_file" in p:
                p["cookies_file"] = ""
            presets.append(p)
    out["presets"] = presets
    return out


def _dumps(data) -> bytes:
    return json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")


def _download_root(download_root: Path | None) -> Path:
    if download_root is not None:
        return Path(download_root)
    from .service import _library_root
    return _library_root(None)


def _collections(download_root: Path | None):
    from . import library
    try:
        return library.collections(_download_root(download_root))
    except OSError:
        return []


def _meta_name(col_id: str, taken: set) -> str:
    base = re.sub(r"[^A-Za-z0-9_-]", "_", col_id)[:100] or "col"
    name, n = base, 2
    while name.lower() in taken:
        name, n = f"{base}-{n}", n + 1
    taken.add(name.lower())
    return f"meta/{name}.json"


def _target(dest: Path) -> Path:
    dest = Path(dest)
    if dest.suffix.lower() == ".zip":
        return dest
    name = f"Bakin-respaldo-{time.strftime('%Y-%m-%d')}"
    out, n = dest / f"{name}.zip", 2
    while out.exists():
        out, n = dest / f"{name} ({n}).zip", n + 1
    return out


# ----------------------------------------------------------------- export

def export(dest: Path, download_root: Path | None = None) -> dict:
    """Write the backup zip into the folder `dest` (Bakin-respaldo-YYYY-MM-DD.zip) or to `dest`
    itself if it ends in .zip. Returns {"path", "files", "size"}."""
    from . import library, music
    base = app_dir()
    try:  # fold what the shell counted while the engine slept into the files first
        music.plays()
        music.skips()
    except OSError:
        pass
    entries: dict[str, bytes] = {}
    for name in _FIXED:
        p = base / name
        if not p.is_file():
            continue
        try:
            raw = p.read_bytes()
        except OSError:
            continue
        if name.endswith(".json"):
            try:
                data = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue
            if name == "media.json" and isinstance(data, dict):
                raw = _dumps(_clean_media(data))
            elif name == "library-folders.json" and isinstance(data, dict):
                raw = _dumps({"folders": [f for f in data.get("folders") or [] if isinstance(f, str)]})
            elif name in ("ui-settings.json", "follows.json", "queue.json", "wallpapers.json"):
                raw = _dumps(_scrub(data))
        entries[name] = raw
    lyr = base / "music" / "lyrics"
    if lyr.is_dir():
        for f in sorted(lyr.iterdir()):
            name = f"music/lyrics/{f.name}"
            if f.is_file() and _LYRIC.fullmatch(name):
                try:
                    entries[name] = f.read_bytes()
                except OSError:
                    pass
    collections, metas, taken = {}, {}, set()
    for col in _collections(download_root):
        try:
            if not col.meta_path.is_file():
                continue
            meta = library.Meta(col)
        except OSError:
            continue
        if not meta.items and not meta.deleted:
            continue
        name = _meta_name(col.id, taken)
        entries[name] = _dumps({"schema": 1, "items": meta.items, "deleted": meta.deleted})
        collections[col.id] = str(col.root)
        metas[col.id] = name
    manifest = {"bakin_version": __version__, "schema": SCHEMA, "id": uuid.uuid4().hex,
                "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "files": sorted(entries),
                "collections": collections, "meta": metas}
    out = _target(dest)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".bakin-backup-", suffix=".tmp", dir=out.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(MANIFEST, _dumps(manifest))
            for name in sorted(entries):
                z.writestr(name, entries[name])
        os.replace(tmp, out)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return {"path": str(out), "files": len(entries), "size": out.stat().st_size}


# ----------------------------------------------------------------- reading

class _Backup:
    """A checked backup zip, read into memory (it's small: no media, only lists and numbers)."""

    def __init__(self, zip_path: Path):
        try:
            z = zipfile.ZipFile(Path(zip_path))
        except (OSError, zipfile.BadZipFile) as exc:
            raise BackupError("Ese archivo no es una copia de Bakin.") from exc
        with z:
            infos = z.infolist()
            names = [i.filename for i in infos]
            if MANIFEST not in names:
                raise BackupError("Ese archivo no es una copia de Bakin.")
            if len(infos) > MAX_ENTRIES or len(set(names)) != len(names) \
                    or sum(i.file_size for i in infos) > MAX_TOTAL or any(i.file_size > MAX_FILE for i in infos):
                raise BackupError("La copia es demasiado grande o está dañada; no se importó nada.")
            bad = [n for n in names if not allowed(n)]
            if bad:
                raise BackupError("La copia trae archivos que Bakin no reconoce; no se importó nada.")
            try:
                self.files = {n: z.read(n) for n in names}
            except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
                raise BackupError("La copia está dañada; no se importó nada.") from exc
        manifest = self.json(MANIFEST)
        if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
            raise BackupError("Ese archivo no es una copia de Bakin.")
        self.manifest = manifest

    def json(self, name: str, default=None):
        raw = self.files.get(name)
        if raw is None:
            return default
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return default

    def dict(self, name: str) -> dict:
        d = self.json(name, {})
        return d if isinstance(d, dict) else {}

    def metas(self) -> list[tuple[str, str, dict]]:
        """(collection id, its path there, its meta) for each collection in the backup."""
        cols = self.manifest.get("collections") or {}
        out = []
        for cid, name in (self.manifest.get("meta") or {}).items():
            data = self.dict(str(name)) if allowed(str(name)) else {}
            if data:
                out.append((str(cid), str(cols.get(cid) or ""), data))
        return out

    def plays(self) -> dict:
        return {str(k): v for k, v in self.dict("music/plays.json").items() if isinstance(v, dict)}

    def playlists(self) -> list[dict]:
        return [p for p in (self.dict("music/playlists.json").get("playlists") or [])
                if isinstance(p, dict) and p.get("id") and p.get("name")]

    def presets(self) -> list[dict]:
        return [p for p in (self.dict("media.json").get("presets") or []) if isinstance(p, dict) and p.get("id")]

    def follows(self) -> list[dict]:
        return [f for f in (self.dict("follows.json").get("follows") or []) if isinstance(f, dict) and f.get("url")]

    def lyrics(self) -> list[str]:
        return [n for n in self.files if _LYRIC.fullmatch(n)]


def _n(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _plural(n: int, one: str, many: str) -> str:
    return f"{n:,}".replace(",", ".") + " " + (one if n == 1 else many)


def inspect(zip_path: Path) -> dict:
    """What a backup holds, without changing anything: {"manifest", "counts", "lines"}
    ("lines" are ready-made Spanish lines for the preview). Raises BackupError."""
    b = _Backup(zip_path)
    plays = b.plays()
    favs = tagged = 0
    tags: set[str] = set()
    for _cid, _path, meta in b.metas():
        for rec in (meta.get("items") or {}).values():
            if not isinstance(rec, dict):
                continue
            favs += bool(rec.get("fav"))
            t = [str(x) for x in rec.get("tags") or []]
            tagged += bool(t)
            tags.update(x.casefold() for x in t)
    counts = {"playlists": len(b.playlists()),
              "plays": sum(_n(v.get("n")) for v in plays.values()),
              "songs_played": sum(1 for v in plays.values() if _n(v.get("n")) > 0),
              "favourites": favs, "tags": len(tags), "tagged": tagged,
              "presets": len(b.presets()), "follows": len(b.follows()), "lyrics": len(b.lyrics()),
              "collections": len(b.metas()),
              "folders": len([f for f in b.dict("library-folders.json").get("folders") or [] if isinstance(f, str)]),
              "settings": bool(b.dict("ui-settings.json"))}
    lines = [_plural(counts["playlists"], "playlist", "playlists"),
             _plural(counts["plays"], "reproducción", "reproducciones") + " de "
             + _plural(counts["songs_played"], "tema", "temas"),
             _plural(counts["favourites"], "favorito", "favoritos"),
             _plural(counts["tags"], "etiqueta", "etiquetas"),
             _plural(counts["presets"], "preset", "presets"),
             _plural(counts["follows"], "seguimiento", "seguimientos"),
             _plural(counts["lyrics"], "letra", "letras")]
    m = b.manifest
    return {"manifest": {"bakin_version": str(m.get("bakin_version") or ""), "schema": m.get("schema"),
                         "id": str(m.get("id") or ""), "created": str(m.get("created") or ""),
                         "files": [str(x) for x in m.get("files") or []],
                         "collections": {str(k): str(v) for k, v in (m.get("collections") or {}).items()}},
            "counts": counts, "lines": lines}


# ----------------------------------------------------------------- import

def _imported_file() -> Path:
    return app_dir() / "backups" / "imported.json"


def _merge_meta(b: _Backup, cols, out: dict) -> None:
    from . import library
    by_id = {c.id: c for c in cols}
    by_path = {str(c.root).lower().rstrip("\\/"): c for c in cols}
    for cid, path, data in b.metas():
        col = by_id.get(cid) or by_path.get(path.lower().rstrip("\\/"))
        if col is None:
            out["collections_skipped"].append(path or cid)
            continue
        meta = library.Meta(col)
        changed = False
        for rel, rec in (data.get("items") or {}).items():
            if not isinstance(rec, dict) or not isinstance(rel, str) or library.resolve(col, rel) is None:
                continue
            cur = dict(meta.items.get(rel) or {})
            before = json.dumps(cur, sort_keys=True)
            if rec.get("fav") and not cur.get("fav"):
                cur["fav"] = True
                out["favourites"] += 1
            have = list(cur.get("tags") or [])
            seen = {t.casefold() for t in have}
            for t in rec.get("tags") or []:
                t = library.clean_tag(t)
                if t and t.casefold() not in seen and len(have) < library.MAX_TAGS:
                    have.append(t)
                    seen.add(t.casefold())
                    out["tags"] += 1
            if have:
                cur["tags"] = have
            for k, v in rec.items():  # anything else a newer Bakin keeps per item
                if k not in ("fav", "tags") and k not in cur and v:
                    cur[k] = v
            if json.dumps(cur, sort_keys=True) != before:
                meta.items[rel] = cur
                changed = True
        if changed:
            meta.save()


def _merge_music(b: _Backup, out: dict, count_plays: bool) -> None:
    from . import music
    mdir = app_dir() / "music"
    if count_plays:
        new = b.plays()
        if new:
            data = {k: dict(v) for k, v in music.plays().items()}
            for item_id, rec in new.items():
                if _n(rec.get("n")) <= 0:
                    continue
                cur = data.setdefault(item_id, {"n": 0, "last": 0})
                cur["n"] = _n(cur.get("n")) + _n(rec.get("n"))
                cur["last"] = max(_f(cur.get("last")), _f(rec.get("last")))
                out["plays"] += _n(rec.get("n"))
            _atomic_json(mdir / "plays.json", data, indent=None)
            music.reset_memory_cache()
        skips_new = {k: v for k, v in b.dict("music/skips.json").items() if isinstance(v, dict)}
        if skips_new:
            data = {k: dict(v) for k, v in music.skips().items()}
            for item_id, rec in skips_new.items():
                if _n(rec.get("n")) <= 0:
                    continue
                cur = data.setdefault(str(item_id), {"n": 0, "last": 0})
                cur["n"] = _n(cur.get("n")) + _n(rec.get("n"))
                cur["last"] = max(_f(cur.get("last")), _f(rec.get("last")))
                out["skips"] += _n(rec.get("n"))
            _atomic_json(mdir / "skips.json", data, indent=None)

    raw_log = b.files.get("music/plays-log.jsonl")
    if raw_log:
        log_path = mdir / "plays-log.jsonl"
        have = {(i, round(at, 1)) for i, at in music.play_log()}
        add = []
        for line in raw_log.decode("utf-8", "replace").splitlines():
            try:
                rec = json.loads(line)
                key = (str(rec["id"]), round(float(rec["at"]), 1))
            except (ValueError, KeyError, TypeError):
                continue
            if key not in have:  # only against what's here: two plays in the same instant there both count
                add.append(key)
        if add:
            try:
                cur = log_path.read_bytes()
            except OSError:
                cur = b""
            if cur and not cur.endswith(b"\n"):
                cur += b"\n"
            lines = "".join(json.dumps({"id": i, "at": at}) + "\n" for i, at in sorted(add, key=lambda x: x[1]))
            _atomic_write(log_path, cur + lines.encode("utf-8"))
            out["plays_log"] += len(add)

    lists = b.playlists()
    if lists:
        mine = music.playlists()
        by_id = {p["id"]: n for n, p in enumerate(mine)}

        def stamp(p):
            return max(_f(p.get("updated")), _f(p.get("modified")), _f(p.get("created")))
        changed = False
        for p in lists:
            if p["id"] not in by_id:
                mine.append(p)
                by_id[p["id"]] = len(mine) - 1
                out["playlists_added"] += 1
                changed = True
            elif stamp(p) > stamp(mine[by_id[p["id"]]]):
                mine[by_id[p["id"]]] = p
                out["playlists_updated"] += 1
                changed = True
        if changed:
            _atomic_json(mdir / "playlists.json", {"playlists": mine}, indent=None)

    loud = {k: v for k, v in b.dict("music/loudness.json").items() if isinstance(v, dict)}
    if loud:
        from . import loudness
        data = loudness.load()
        new = {k: v for k, v in loud.items() if k not in data}
        if new:
            _atomic_json(mdir / "loudness.json", {**data, **new}, indent=None)
            out["loudness"] += len(new)

    for name in b.lyrics():
        dest = app_dir() / name
        if not dest.exists():
            _atomic_write(dest, b.files[name])
            out["lyrics"] += 1


def _merge_settings(b: _Backup, out: dict) -> None:
    base = app_dir()
    new = _scrub(b.dict("ui-settings.json"))
    if new:
        cur = _read_json(base / "ui-settings.json", {})
        cur = cur if isinstance(cur, dict) else {}
        add = {}
        for k, v in new.items():
            if k in cur:
                continue
            if k == "out_dir" and not (isinstance(v, str) and Path(v).is_dir()):
                continue  # a folder of the other PC
            add[k] = v
        if add:
            _atomic_json(base / "ui-settings.json", {**cur, **add})
            out["settings"] += len(add)

    media = b.dict("media.json")
    if media:
        from . import mediadl
        ms = mediadl.load_settings()
        have = {p["id"] for p in ms["presets"]}
        changed = False
        for p in media.get("presets") or []:
            if not isinstance(p, dict) or not p.get("id"):
                continue
            p = mediadl.clean_preset(p)
            if p["id"] in have:
                continue
            p["cookies_file"] = ""
            if p["cookies_source"] in ("file", "vault"):  # nothing to log in with here
                p["cookies_source"], p["cookies_mode"] = "browser", "off"
            ms["presets"].append(p)
            have.add(p["id"])
            out["presets"] += 1
            changed = True
        prefs = media.get("site_prefs") if isinstance(media.get("site_prefs"), dict) else {}
        for site, pid in prefs.items():
            if site not in ms["site_prefs"] and pid in have:
                ms["site_prefs"][site] = pid
                changed = True
        if changed:
            ms["site_prefs"] = mediadl._clean_site_prefs(ms["site_prefs"], have)
            _atomic_json(base / "media.json", ms)

    follows = b.follows()
    if follows:
        path = base / "follows.json"
        data = _read_json(path, {})
        mine = [f for f in (data.get("follows") if isinstance(data, dict) else None) or []
                if isinstance(f, dict) and f.get("url")]
        urls = {str(f["url"]).rstrip("/").lower() for f in mine}
        ids = {f.get("id") for f in mine}
        added = 0
        for f in follows:
            key = str(f["url"]).rstrip("/").lower()
            if key in urls:
                continue
            f = _scrub(dict(f))
            if f.get("id") in ids or not f.get("id"):
                f["id"] = uuid.uuid4().hex[:10]
            mine.append(f)
            urls.add(key)
            ids.add(f["id"])
            added += 1
        if added:
            _atomic_json(path, {**(data if isinstance(data, dict) else {}), "follows": mine})
            out["follows"] += added

    if "wallpapers.json" in b.files and not (base / "wallpapers.json").exists():
        _atomic_json(base / "wallpapers.json", _scrub(b.json("wallpapers.json", {})))


def _merge_folders(b: _Backup, download_root: Path | None, out: dict) -> None:
    """Folders added to the library there that exist here too (so their favourites come along)."""
    from . import library
    folders = [f for f in b.dict("library-folders.json").get("folders") or [] if isinstance(f, str)]
    if not folders:
        return
    root = _download_root(download_root)
    have = {str(p).lower() for p in library.user_folders()}
    for f in folders:
        p = Path(f)
        if str(p).lower() in have:
            continue
        if library.check_new_folder(p, root) is None:
            library.save_user_folders(library.user_folders() + [p])
            have.add(str(p).lower())
            out["folders"] += 1


def import_(zip_path: Path, mode: str = "merge", download_root: Path | None = None) -> dict:
    """Merge a backup into this Bakin (never deletes anything). A copy of the current state goes
    to backups/before-import-<time>.zip first. Returns what changed. Raises BackupError."""
    if mode != "merge":
        raise BackupError("Por ahora solo se puede combinar la copia con lo que ya hay.")
    b = _Backup(zip_path)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safety = export(app_dir() / "backups" / f"before-import-{stamp}.zip", download_root)
    out = {"safety": safety["path"], "already_imported": False, "favourites": 0, "tags": 0,
           "plays": 0, "plays_log": 0, "skips": 0, "playlists_added": 0, "playlists_updated": 0,
           "presets": 0, "follows": 0, "lyrics": 0, "settings": 0, "loudness": 0, "folders": 0,
           "collections_skipped": []}
    # The same copy imported twice must not count its plays twice (the rest is idempotent anyway).
    done = _read_json(_imported_file(), {})
    done = done if isinstance(done, dict) else {}
    seen = [str(x) for x in done.get("ids") or []]
    bid = str(b.manifest.get("id") or "")
    out["already_imported"] = bool(bid) and bid in seen
    _merge_settings(b, out)
    _merge_folders(b, download_root, out)
    _merge_meta(b, _collections(download_root), out)
    _merge_music(b, out, count_plays=not out["already_imported"])
    if bid and bid not in seen:
        _atomic_json(_imported_file(), {"ids": (seen + [bid])[-500:]})
    return out
