"""The library: what Bakin downloaded plus any folders the user adds.

Collections
* every folder Bakin downloads into (music presets, Wallhaven: "md:<folder>");
* every folder the user adds ("dir:<hash of its path>"), any file type.

Favourites and tags live in a small JSON file per collection: inside the
folder for Bakin's own (so they travel when the downloads are moved) and in
Bakin's private data folder for the user's own folders (so Bakin never
writes into them).

Deleting sends files to the Windows Recycle Bin, never deletes them outright:
on a drive without a Recycle Bin (network, some removable drives) Bakin
refuses. Restoring puts the file back from the Recycle Bin.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import io
import json
import os
import re
import struct
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .paths import app_dir, is_inside

IMAGE = {"jpg", "jpeg", "png", "webp", "gif", "bmp", "tif", "tiff", "avif", "heic", "heif", "jfif"}
VIDEO = {"mp4", "m4v", "mov", "webm", "mkv", "avi", "wmv", "mpg", "mpeg", "3gp"}
AUDIO = {"mp3", "wav", "flac", "m4a", "aac", "ogg", "oga", "opus", "wma", "aif", "aiff"}
KINDS = ("image", "video", "audio", "other")
THUMBABLE = {"jpg", "jpeg", "png", "webp", "gif", "bmp", "tif", "tiff", "jfif"}

META_FILE = ".bakin-meta.json"
SKIP_DIRS = {".partial", "$recycle.bin", "system volume information"}
SKIP_FILES = {META_FILE, "desktop.ini", "thumbs.db", "archive.txt"}
SKIP_SUFFIXES = (".tmp", ".part", ".ytdl", ".temp")
THUMB_PX = 420
MAX_TAGS = 30


def kind_of(ext: str) -> str:
    ext = ext.lower()
    return "image" if ext in IMAGE else "video" if ext in VIDEO else "audio" if ext in AUDIO else "other"


# ------------------------------------------------------------- collections

@dataclass(frozen=True)
class Collection:
    id: str
    kind: str        # "media" | "folder"
    name: str
    root: Path

    @property
    def meta_path(self) -> Path:
        if self.kind == "media":
            return self.root / META_FILE
        return app_dir() / "library" / f"{self.id.split(':', 1)[1]}.json"


def folder_id(path: Path) -> str:
    return "dir:" + hashlib.sha1(str(path).lower().encode("utf-8")).hexdigest()[:12]


def _folders_file() -> Path:
    return app_dir() / "library-folders.json"


def user_folders() -> list[Path]:
    try:
        data = json.loads(_folders_file().read_text(encoding="utf-8"))
        return [Path(p) for p in data.get("folders", []) if isinstance(p, str)]
    except (OSError, ValueError, AttributeError):
        return []


def _folders_data() -> dict:
    try:
        data = json.loads(_folders_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_user_folders(folders: list[Path]) -> None:
    data = _folders_data()
    _atomic_json(_folders_file(), {**data, "folders": [str(p) for p in folders]})


def sync_preset_folders(download_root: Path, presets: list[dict]) -> list[str]:
    """Music/video presets that save outside the download folder: show those
    folders in the library too. Each is added once; if the user removes it
    from the library it is not added back."""
    data = _folders_data()
    offered = {str(x).lower() for x in data.get("offered", [])}
    added = []
    for preset in presets:
        f = Path(str(preset.get("folder") or "")).expanduser()
        if not f.is_absolute() or not f.is_dir():
            continue
        f = f.resolve()
        if str(f).lower() in offered:
            continue
        offered.add(str(f).lower())
        if check_new_folder(f, download_root) is None:
            save_user_folders(user_folders() + [f])
            added.append(str(f))
    data = _folders_data()
    _atomic_json(_folders_file(), {**data, "offered": sorted(offered)})
    return added


def collections(download_root: Path) -> list[Collection]:
    from .relocate import accounts
    out = []
    for d in accounts(download_root):
        out.append(Collection(f"md:{d.name}", "media", d.name, d))  # a preset's or Wallhaven's folder
    for p in user_folders():
        out.append(Collection(folder_id(p), "folder", p.name or str(p), p))
    return out


def check_new_folder(path: Path, download_root: Path) -> str | None:
    if not path.is_absolute():
        return "Usa una ruta completa."
    if not path.is_dir():
        return "Esa carpeta no existe."
    if is_inside(path, app_dir()) or is_inside(app_dir(), path):
        return "Esa carpeta contiene los datos privados de Bakin."
    if is_inside(path, download_root) or is_inside(download_root, path):
        return "Esa carpeta se superpone con la carpeta de descargas, que ya está en la biblioteca."
    for other in user_folders():
        if is_inside(path, other) or is_inside(other, path):
            return f"Se superpone con {other}, que ya está en la biblioteca."
    return None


# -------------------------------------------------------------------- meta

def _atomic_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Meta:
    """Favourites, tags and deletions of one collection, keyed by relative path."""

    def __init__(self, col: Collection):
        self.path = col.meta_path
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.items: dict[str, dict] = data.get("items") or {}
        self.deleted: dict[str, dict] = data.get("deleted") or {}

    def get(self, rel: str) -> dict:
        return self.items.get(rel) or {}

    def update(self, rel: str, **kw) -> None:
        rec = {**self.get(rel), **kw}
        rec = {k: v for k, v in rec.items() if v}  # drop fav=False / tags=[]
        if rec:
            self.items[rel] = rec
        else:
            self.items.pop(rel, None)

    def save(self) -> None:
        _atomic_json(self.path, {"schema": 1, "items": self.items, "deleted": self.deleted})


def clean_tag(tag: str) -> str:
    return " ".join(str(tag).split())[:40]


# -------------------------------------------------------------------- scan

def scan(col: Collection) -> list[dict]:
    """Every file of a collection, with its favourite and tags."""
    if not col.root.is_dir():
        return []
    meta = Meta(col)
    items = []
    changed = False
    for base, dirs, names in os.walk(col.root):
        dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS and not d.startswith(".")]
        covers = _cover_stems(names) if col.kind == "media" else set()
        for name in names:
            if covers and Path(name).suffix[1:].lower() in IMAGE and Path(name).stem.lower() in covers:
                continue  # a song's or video's cover, not a photo (yt-dlp keeps it there while it downloads)
            if name.lower() in SKIP_FILES or name.startswith(".") or name.lower().endswith(SKIP_SUFFIXES) \
                    or ".part-frag" in name or re.search(r"\.f\d+\.\w+$", name):
                continue
            p = Path(base) / name
            try:
                st = p.stat()
            except OSError:
                continue
            rel = p.relative_to(col.root).as_posix()
            if rel in meta.deleted:  # put back by hand (e.g. from the Recycle Bin)
                meta.deleted.pop(rel)
                changed = True
            ext = p.suffix[1:].lower()
            m = meta.get(rel)
            item = {"id": f"{col.id}|{rel}", "col": col.id, "rel": rel, "name": name,
                    "path": str(p), "kind": kind_of(ext), "ext": ext, "size": st.st_size,
                    "mtime": int(st.st_mtime), "date": int(st.st_mtime),
                    "fav": bool(m.get("fav")), "tags": list(m.get("tags") or [])}
            if item["kind"] == "image" and ext in THUMBABLE:
                from . import imageinfo
                d = imageinfo.dims(item["path"], item["size"], item["mtime"])
                if d:
                    item["width"], item["height"] = d
            if item["kind"] == "audio":
                from . import audiotags
                a = audiotags.info(item["path"], item["size"], item["mtime"])
                item.update(title=a["title"], artist=a["artist"], album=a["album"], duration=a["duration"],
                            bitrate=a["bitrate"], codec=a["codec"], lossless=a["lossless"], yt_id=a["yt_id"],
                            low_quality=audiotags.is_low_quality(a))
            items.append(item)
    if changed:
        meta.save()
    from . import audiotags, imageinfo
    audiotags.flush()
    imageinfo.flush()
    return items


def _cover_stems(names: list[str]) -> set[str]:
    """In a music or video folder, the names an image would have if it were the cover of one
    of its songs or videos, finished or still downloading («Tema.opus», «Tema.webm.part»,
    «Tema.f251.webm.part»): yt-dlp writes «Tema.webp» next to it and embeds it at the end."""
    stems: set[str] = set()
    for name in names:
        parts = name.lower().split(".")
        if len(parts) < 2:
            continue
        exts = parts[1:]
        if not any(e in AUDIO or e in VIDEO or e in ("part", "ytdl", "temp", "tmp") for e in exts):
            continue
        for i in range(1, len(parts)):  # every prefix: «tema.webm», «tema»
            stems.add(".".join(parts[:i]))
    return stems


def query(items: list[dict], f: dict) -> list[dict]:
    cols = set(f.get("collections") or [])
    kinds = set(f.get("kinds") or [])
    tags = [t.lower() for t in (f.get("tags") or [])]
    q = str(f.get("q") or "").strip().lower()
    # «Fondos» filters (PLAN 0.13.3): only images whose size is known
    orient = f.get("orient") if f.get("orient") in ("landscape", "portrait", "square") else None
    try:
        min_px = max(0, int(f.get("min_px") or 0))
    except (TypeError, ValueError):
        min_px = 0
    color = str(f.get("color") or "").lstrip("#").lower() or None
    if orient or min_px or color:
        from . import imageinfo
    out = []
    for it in items:
        if orient or min_px or color:
            w, h = it.get("width"), it.get("height")
            if it["kind"] != "image" or not w:
                continue
            if orient and imageinfo.orientation(w, h) != orient:
                continue
            if min_px and min(w, h) < min_px:
                continue
            if color and imageinfo.color(it["path"], it["size"], it["mtime"]) != color:
                continue
        if cols and it["col"] not in cols:
            continue
        if kinds and it["kind"] not in kinds:
            continue
        if f.get("favorites") and not it["fav"]:
            continue
        if tags:
            mine = {t.lower() for t in it["tags"]}
            if not all(t in mine for t in tags):
                continue
        if f.get("low_quality") and not it.get("low_quality"):
            continue
        if q and q not in it["name"].lower() and not any(q in t.lower() for t in it["tags"])                 and not any(q in str(it.get(k) or "").lower() for k in ("title", "artist", "album")):
            continue
        out.append(it)
    sort = f.get("sort") or "new"
    if sort == "old":
        out.sort(key=lambda i: (i["date"], i["rel"]))
    elif sort == "name":
        out.sort(key=lambda i: i["name"].lower())
    elif sort == "size":
        out.sort(key=lambda i: -i["size"])
    elif sort in ("title", "artist", "album"):
        # Empty values last; then by the rest so albums keep a stable order.
        def key(i, k=sort):
            v = str(i.get(k) or "").casefold()
            return (v == "", v, str(i.get("album") or "").casefold(), str(i.get("title") or i["name"]).casefold())
        out.sort(key=key)
    elif sort in ("duration", "bitrate"):
        out.sort(key=lambda i, k=sort: (i.get(k) is None, -(i.get(k) or 0)))
    elif sort == "bitrate_asc":
        out.sort(key=lambda i: (i.get("bitrate") is None, i.get("bitrate") or 0))
    else:  # newest first; carousel slides keep their order
        out.sort(key=lambda i: (-i["date"], i["rel"]))
    return out


# ------------------------------------------------------------------ thumbs

def thumb_path(item: dict, px: int = THUMB_PX) -> Path:
    """Where an item's preview at `px` is cached (the shell reads the same, covers.rs)."""
    key = hashlib.sha1(f"{item['path']}|{item['size']}|{item['mtime']}|{px}".encode("utf-8")).hexdigest()
    return app_dir() / "thumbs" / key[:2] / f"{key}.jpg"


def thumb(item: dict, size: int = THUMB_PX) -> str:
    """Cached JPEG preview (data URI) of an image, or of the cover art
    embedded in an audio file; "" otherwise. Cached under Bakin's private
    data folder; the original is only read."""
    path = thumb_file(item, size)
    if not path:
        return ""
    try:
        return "data:image/jpeg;base64," + base64.b64encode(Path(path).read_bytes()).decode()
    except OSError:
        return ""


def thumb_file(item: dict, size: int = THUMB_PX) -> str:
    """The same preview as a file in the cache (made if needed), for the page to load through the
    shell's asset: protocol (PLAN 0.17.1: thousands of base64 previews in the list's JSON weighed
    on the window's memory); "" if there's none."""
    audio = item.get("kind") == "audio"
    if item["ext"] not in THUMBABLE and not audio:
        return ""
    cached = lambda px: thumb_path(item, px)  # noqa: E731
    cache = cached(size)
    if cache.is_file():
        return str(cache)
    try:
        from PIL import Image
        # a bigger preview already made (covers are cached at 320 px, photos at THUMB_PX)
        bigger = next((c for c in (cached(px) for px in (320, THUMB_PX) if px > size) if c.is_file()), None)
        if bigger is not None:
            src = bigger  # a bigger preview is already made: shrinking it is quick
        elif audio:
            from . import audiotags
            art = audiotags.cover(Path(item["path"]))
            if not art:
                raise ValueError("no cover")
            src = io.BytesIO(art)
        else:
            src = item["path"]
        with Image.open(src) as im:
            im.draft("RGB", (size * 2, size * 2))
            im = im.convert("RGB")
            if audio:  # YouTube thumbnails are 16:9 with bars: keep the square middle
                w, h = im.size
                if w > h * 1.2:
                    im = im.crop(((w - h) // 2, 0, (w + h) // 2, h))
            im.thumbnail((size, size))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=88 if audio else 82)
        data = buf.getvalue()
    except Exception:
        return ""
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(data)
    except OSError:
        return ""
    return str(cache)


# ------------------------------------------------------------ recycle bin

class RecycleError(Exception):
    pass


def _drive_has_recycle_bin(path: Path) -> bool:
    if os.name != "nt":
        return False
    drive = path.drive
    if not drive or drive.startswith("\\\\"):
        return False  # network share: deleting there is permanent
    return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 3  # DRIVE_FIXED


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p), ("wFunc", ctypes.c_uint), ("pFrom", ctypes.c_wchar_p),
                ("pTo", ctypes.c_wchar_p), ("fFlags", ctypes.c_ushort),
                ("fAnyOperationsAborted", ctypes.c_int), ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", ctypes.c_wchar_p)]


def send_to_recycle_bin(path: Path) -> None:
    """Move one file to the Recycle Bin. Raises RecycleError, never deletes outright."""
    if not _drive_has_recycle_bin(path):
        raise RecycleError("esta unidad no tiene Papelera de reciclaje; no se borró")
    FO_DELETE = 3
    flags = 0x40 | 0x10 | 0x04 | 0x400 | 0x4000  # ALLOWUNDO, NOCONFIRMATION, SILENT, NOERRORUI, WANTNUKEWARNING
    op = _SHFILEOPSTRUCTW(None, FO_DELETE, str(path) + "\0", None, flags, 0, None, None)
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc or op.fAnyOperationsAborted:
        raise RecycleError(f"Windows no pudo enviarlo a la Papelera (código {rc})")
    if path.exists():
        raise RecycleError("sigue en su lugar")


def _read_info(i_file: Path) -> tuple[str, int] | None:
    """Original path and deletion time from a Recycle Bin $I file."""
    try:
        raw = i_file.read_bytes()
        version, _size, deleted = struct.unpack_from("<qqq", raw, 0)
        if version == 2:
            (n,) = struct.unpack_from("<i", raw, 24)
            original = raw[28:28 + 2 * n].decode("utf-16-le")
        elif version == 1:
            original = raw[24:24 + 520].decode("utf-16-le")
        else:
            return None
    except (OSError, struct.error, UnicodeDecodeError):
        return None
    return original.split("\0", 1)[0], deleted


def recycle_bin_index(bin_root: Path) -> dict[str, Path]:
    """Every file in a drive's Recycle Bin by its original path (lower case),
    the most recently deleted one if several. Read once for a whole list:
    looking each file up on its own read the bin once per file."""
    best: dict[str, tuple[int, Path]] = {}
    try:
        sids = [d for d in bin_root.iterdir() if d.is_dir()]
    except OSError:
        return {}
    for sid in sids:
        try:
            infos = list(sid.glob("$I*"))
        except OSError:
            continue  # another user's bin
        for info in infos:
            parsed = _read_info(info)
            if not parsed:
                continue
            key = parsed[0].lower()
            if key in best and best[key][0] >= parsed[1]:
                continue
            data = info.with_name("$R" + info.name[2:])
            if data.exists():
                best[key] = (parsed[1], data)
    return {k: v[1] for k, v in best.items()}


def bin_root_of(original: Path) -> Path:
    """The Recycle Bin of the drive a file was on (tests point it elsewhere)."""
    return Path(original.drive + "\\$Recycle.Bin")


def find_in_recycle_bin(original: Path, bin_root: Path | None = None) -> Path | None:
    """The $R file holding `original`, the most recently deleted one if several."""
    bin_root = bin_root or bin_root_of(original)
    return recycle_bin_index(bin_root).get(str(original).lower())


def restore_from_recycle_bin(original: Path, bin_root: Path | None = None) -> None:
    if original.exists():
        raise RecycleError("ya hay un archivo en esa ubicación")
    data = find_in_recycle_bin(original, bin_root)
    if data is None:
        raise RecycleError("ya no está en la Papelera de Windows (¿se vació?)")
    original.parent.mkdir(parents=True, exist_ok=True)
    os.rename(data, original)
    data.with_name("$I" + data.name[2:]).unlink(missing_ok=True)


def delete_items(col: Collection, rels: list[str]) -> tuple[list[str], list[dict]]:
    """Send files to the Recycle Bin and remember them. Returns (done, failed)."""
    done, failed = [], []
    meta = Meta(col)
    for rel in rels:
        p = resolve(col, rel)
        if p is None or not p.is_file():
            failed.append({"rel": rel, "reason": "no se encontró"})
            continue
        size = p.stat().st_size
        try:
            send_to_recycle_bin(p)
        except RecycleError as exc:
            failed.append({"rel": rel, "reason": str(exc)})
            continue
        meta.deleted[rel] = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                             "path": str(p), "size": size}
        done.append(rel)
    if done:
        meta.save()
    return done, failed


def resolve(col: Collection, rel: str) -> Path | None:
    rel = str(rel)
    if not rel or rel.startswith(("/", "\\")) or ":" in rel or ".." in Path(rel).parts:
        return None
    p = col.root / rel
    return p if is_inside(p, col.root) else None
