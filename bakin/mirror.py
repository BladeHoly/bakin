"""Playlists espejo: keep a folder (a car stick, a phone, another player)
equal to a playlist.

The folder gets one copy (or hard link) of each song, named after the source
file, plus a ``playlist.m3u8`` with relative paths. What Bakin put there is
listed in a manifest (``.bakin-mirror.json``) in the folder itself, so a
later sync only ever removes audio files Bakin wrote: anything else the user
keeps in that folder is never touched.

Two steps: ``plan`` compares (nothing is written), ``apply`` does it.
Removals go through a ``trash`` function the caller passes (the Recycle Bin
in the app, a fake in tests); this module never deletes a song outright.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Callable

from .library import AUDIO, VIDEO
from .paths import is_inside

MANIFEST = ".bakin-mirror.json"
PLAYLIST = "playlist.m3u8"
PLAYLIST_ALT = "playlist (Bakin).m3u8"
VERSION = 1
MEDIA = AUDIO | VIDEO
MTIME_SLACK = 2.0  # FAT/exFAT sticks keep times in 2-second steps
MODES = ("copy", "hardlink")


class MirrorError(Exception):
    """A folder Bakin won't mirror into (the message is for the user)."""


# ---------------------------------------------------------------- manifest

def _manifest_path(folder: Path) -> Path:
    return Path(folder) / MANIFEST


def read_manifest(folder: Path) -> dict | None:
    """The folder's manifest, or None when there is none (or it's unreadable)."""
    try:
        data = json.loads(_manifest_path(folder).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        return None
    data.setdefault("adopted", [])
    data.setdefault("removals", 0)
    data.setdefault("m3u8", PLAYLIST)
    return data


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".bakin-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fp:
            fp.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _write_manifest(folder: Path, man: dict) -> None:
    man = dict(man, version=VERSION, updated=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    _atomic_write(_manifest_path(folder), json.dumps(man, ensure_ascii=False, indent=1).encode("utf-8"))


def first_removal(folder: Path) -> bool:
    """True when this mirror folder has never had a removal (the UI asks first)."""
    man = read_manifest(folder)
    return not man or not int(man.get("removals") or 0)


# ---------------------------------------------------------------- plan

def _key(name: str) -> str:
    return name.casefold()  # Windows (and FAT sticks) ignore case


def _numbered(name: str, n: int) -> str:
    p = Path(name)
    return f"{p.stem} ({n}){p.suffix}"


def _check_folder(folder: Path, songs: list[dict], forbidden: list[Path]) -> None:
    for root in forbidden:
        root = Path(root)
        if is_inside(folder, root):
            raise MirrorError("Esa carpeta está dentro de la biblioteca de música; elige otra.")
        if is_inside(root, folder):
            raise MirrorError("Esa carpeta contiene la biblioteca de música; elige otra.")
    for s in songs:
        if is_inside(Path(s["path"]), folder):
            raise MirrorError("Esa carpeta contiene canciones de la lista; elige otra.")


def _src_stat(song: dict) -> tuple[int, float] | None:
    size, mtime = song.get("size"), song.get("mtime")
    if size is not None and mtime is not None:
        return int(size), float(mtime)
    try:
        st = Path(song["path"]).stat()
    except OSError:
        return None
    return st.st_size, st.st_mtime


def _who(song: dict) -> str:
    return " - ".join(x for x in (song.get("artist"), song.get("title")) if x) \
        or Path(song.get("name") or song["path"]).stem


def plan(songs: list[dict], folder: Path, mode: str = "copy", forbidden: list[Path] | None = None,
         adopt: bool = False) -> dict:
    """What a sync would do. Nothing is written.

    ``songs``: the playlist in order, each {"id", "path", "name", "size",
    "mtime", "title", "artist"} (optionally "duration" in seconds).
    ``forbidden``: the library's music folders (the mirror can't be inside
    one, nor contain one). A non-empty folder without a manifest is refused
    unless ``adopt``: then what's already there is left alone for good.
    """
    if mode not in MODES:
        raise MirrorError("Modo desconocido.")
    folder = Path(folder)
    if folder.exists() and not folder.is_dir():
        raise MirrorError("Eso no es una carpeta.")
    _check_folder(folder, songs, list(forbidden or []))

    man = read_manifest(folder)
    try:
        present = {_key(p.name): p.name for p in folder.iterdir()} if folder.is_dir() else {}
    except OSError as exc:
        raise MirrorError(f"No se pudo leer la carpeta: {exc.strerror or exc}") from exc
    adopted: list[str] = []
    if man is None:
        if present and not adopt:
            raise MirrorError("La carpeta no está vacía. Elige una vacía, o úsala igual: "
                              "lo que ya tiene no se va a tocar nunca.")
        adopted = sorted(present.values())
        man = {"files": {}, "adopted": adopted, "removals": 0, "m3u8": PLAYLIST}
    else:
        adopted = list(man.get("adopted") or [])
    ours = {_key(n): n for n in man["files"]}
    # names Bakin must never take: whatever is in the folder and isn't ours
    foreign = {k for k in present if k not in ours} | {_key(n) for n in adopted}
    foreign.discard(_key(MANIFEST))
    m3u8_name = man.get("m3u8") or PLAYLIST
    if _key(m3u8_name) not in {_key(n) for n in adopted}:
        foreign.discard(_key(m3u8_name))  # our own playlist file from the last sync

    if _key(m3u8_name) in foreign:
        m3u8_name = PLAYLIST_ALT if _key(PLAYLIST_ALT) not in foreign else None
    taken = set(foreign) | {_key(MANIFEST)}
    if m3u8_name:
        taken.add(_key(m3u8_name))

    # 1) a song keeps the name it already has in the folder
    by_id: dict[str, str] = {}
    for name, e in man["files"].items():
        by_id.setdefault(str(e.get("id")), name)
    names: dict[int, str] = {}
    for i, s in enumerate(songs):
        old = by_id.get(str(s["id"]))
        base = Path(s["path"]).name
        if old and _key(old) not in taken and (old == base or Path(old).suffix == Path(base).suffix
                                                and Path(old).stem.startswith(Path(base).stem + " (")):
            names[i] = old
            taken.add(_key(old))
    # 2) the others get the source's name, or "name (2)" and on
    for i, s in enumerate(songs):
        if i in names:
            continue
        base = Path(s["path"]).name
        name, n = base, 1
        while _key(name) in taken:
            n += 1
            name = _numbered(base, n)
        names[i] = name
        taken.add(_key(name))

    copy, update, missing, entries = [], [], [], {}
    keep = 0
    for i, s in enumerate(songs):
        name = names[i]
        st = _src_stat(s)
        if st is None:
            missing.append({"id": s["id"], "file": s["path"]})
            continue
        size, mtime = st
        dst = folder / name
        job = {"id": s["id"], "src": str(s["path"]), "dst": str(dst)}
        entries[name] = {"id": s["id"], "src": str(s["path"])}
        try:
            d = dst.stat()
        except OSError:
            copy.append(job)
            continue
        if _key(name) not in ours:  # can't happen (taken), but never overwrite a stranger
            copy.append(job)
        elif d.st_size != size or d.st_mtime < mtime - MTIME_SLACK:
            update.append(job)
        else:
            keep += 1

    keep_keys = {_key(n) for n in entries}
    remove = []
    for name in man["files"]:
        k = _key(name)
        if k in keep_keys or k in foreign or Path(name).suffix.lower().lstrip(".") not in MEDIA:
            continue
        p = folder / name
        if p.is_file():
            remove.append(str(p))

    lines = ["#EXTM3U"]
    for i, s in enumerate(songs):
        if names[i] in entries:
            dur = s.get("duration")
            lines.append(f"#EXTINF:{int(dur) if dur else -1},{_who(s)}")
            lines.append(names[i])
    m3u8 = "\n".join(lines) + "\n"
    m3u8_changed = True
    if m3u8_name:
        try:
            m3u8_changed = (folder / m3u8_name).read_text(encoding="utf-8") != m3u8
        except OSError:
            pass

    return {"folder": str(folder), "mode": mode, "copy": copy, "update": update, "remove": remove,
            "keep": keep, "missing": missing, "m3u8": m3u8, "m3u8_name": m3u8_name,
            "m3u8_changed": m3u8_changed and bool(m3u8_name), "entries": entries,
            "adopted": adopted, "order": [names[i] for i in range(len(songs)) if names[i] in entries],
            "first_removal": first_removal(folder) and bool(remove)}


def is_empty(p: dict) -> bool:
    """Nothing to do: the folder already matches the playlist."""
    return not (p["copy"] or p["update"] or p["remove"] or p.get("m3u8_changed"))


# ---------------------------------------------------------------- apply

def _place(src: Path, dst: Path, mode: str) -> None:
    """Put src at dst atomically (temp file in the folder, then os.replace)."""
    fd, tmp = tempfile.mkstemp(prefix=".bakin-", suffix=".part", dir=dst.parent)
    os.close(fd)
    tmp = Path(tmp)
    try:
        linked = False
        if mode == "hardlink":
            tmp.unlink()
            try:
                os.link(src, tmp)
                linked = True
            except OSError:  # another drive, FAT, no rights: copy instead
                pass
        if not linked:
            shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def apply(plan: dict, folder: Path, trash: Callable[[list[Path]], None],
          progress: Callable[[int, int, str], None] | None = None,
          cancel: Callable[[], bool] | None = None) -> dict:
    """Do what ``plan`` said. Removals first (frees room on a full stick),
    then copies and updates, then playlist.m3u8. The manifest is rewritten
    after every file, so it always matches what's in the folder, also when
    cancelled (``cancel()`` is checked between files).

    ``trash(paths)`` must send those files somewhere recoverable (or raise);
    whatever still exists afterwards counts as failed.
    """
    folder = Path(folder)
    if Path(plan["folder"]) != folder:
        raise MirrorError("El plan es de otra carpeta.")
    folder.mkdir(parents=True, exist_ok=True)
    man = read_manifest(folder) or {"files": {}, "adopted": list(plan.get("adopted") or []),
                                    "removals": 0, "m3u8": plan.get("m3u8_name") or PLAYLIST}
    files: dict = man["files"]
    # forget entries the user already deleted by hand
    for name in [n for n in files if not (folder / n).exists()]:
        del files[name]
    _write_manifest(folder, man)

    out = {"copied": 0, "updated": 0, "removed": 0, "failed": [], "cancelled": False}
    jobs = [("copy", j) for j in plan["copy"]] + [("update", j) for j in plan["update"]]
    total = len(jobs) + (1 if plan["remove"] else 0)
    step = 0

    def stopped() -> bool:
        if cancel and cancel():
            out["cancelled"] = True
        return out["cancelled"]

    if plan["remove"] and not stopped():
        paths = [Path(p) for p in plan["remove"]]
        # never anything outside the folder or not ours, whatever the plan says
        own = {_key(n): n for n in files}
        paths = [p for p in paths if p.parent == folder and _key(p.name) in own]
        if progress:
            progress(step, total, "")
        try:
            trash(paths)
        except Exception as exc:  # noqa: BLE001 - reported per file below
            err = str(exc) or type(exc).__name__
        else:
            err = "no se pudo enviar a la Papelera"
        gone = 0
        for p in paths:
            if p.exists():
                out["failed"].append({"file": str(p), "error": err})
            else:
                files.pop(own[_key(p.name)], None)
                gone += 1
        out["removed"] = gone
        if gone:
            man["removals"] = int(man.get("removals") or 0) + 1
        _write_manifest(folder, man)
        step += 1

    for kind, job in jobs:
        if stopped():
            break
        src, dst = Path(job["src"]), Path(job["dst"])
        if progress:
            progress(step, total, dst.name)
        try:
            if dst.parent != folder:
                raise MirrorError("destino fuera de la carpeta")
            _place(src, dst, plan.get("mode", "copy"))
        except (OSError, MirrorError) as exc:
            out["failed"].append({"file": str(src), "error": getattr(exc, "strerror", None) or str(exc)})
        else:
            files[dst.name] = {"id": job["id"], "src": str(src)}
            out["copied" if kind == "copy" else "updated"] += 1
            _write_manifest(folder, man)
        step += 1

    # the playlist lists only what's really there (all of it, unless cancelled)
    name = plan.get("m3u8_name")
    if name:
        have = {_key(n) for n in files}
        lines, src_lines = ["#EXTM3U"], plan["m3u8"].splitlines()[1:]
        for info, entry in zip(src_lines[0::2], src_lines[1::2]):
            if _key(entry) in have:
                lines += [info, entry]
        try:
            _atomic_write(folder / name, ("\n".join(lines) + "\n").encode("utf-8"))
            man["m3u8"] = name
        except OSError as exc:
            out["failed"].append({"file": str(folder / name), "error": exc.strerror or str(exc)})
    _write_manifest(folder, man)
    if progress:
        progress(total, total, "")
    return out
