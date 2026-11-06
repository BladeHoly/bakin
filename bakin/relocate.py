"""Move Bakin's download folders to another download folder, and back.

Only folders Bakin made (a direct child holding its .bakin-media.json mark:
a music preset's folder, Wallhaven) are moved; anything else in the old folder stays where it is. A folder is moved
with a rename when both places are on the same drive. Across drives every
file is copied, read back and compared by SHA-256 before the original is
deleted, so an interrupted or failed move never loses a photo: the original
stays complete until its copy is proven identical.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .paths import app_dir, is_inside

MEDIA_MARKER = ".bakin-media.json"  # a folder Bakin downloads into (see mediadl.py)

CHUNK = 1 << 20
STAGING = ".bakin-moving"   # suffix of a half-copied folder (cross-drive only)
SKIP_NAMES = {".partial"}


def history_file() -> Path:
    return app_dir() / "library-moves.json"


def last_move() -> dict | None:
    try:
        data = json.loads(history_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("from") and data.get("to") else None


def save_move(record: dict | None) -> None:
    path = history_file()
    if record is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")


def accounts(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(d for d in root.iterdir()
                  if d.is_dir() and not d.name.endswith(STAGING)
                  and (d / MEDIA_MARKER).is_file())


def folder_size(d: Path) -> tuple[int, int]:
    files = size = 0
    for base, dirs, names in os.walk(d):
        dirs[:] = [x for x in dirs if x not in SKIP_NAMES]
        for n in names:
            if n in SKIP_NAMES:
                continue
            try:
                size += (Path(base) / n).stat().st_size
                files += 1
            except OSError:
                pass
    return files, size


def check_target(src: Path, dst: Path) -> str | None:
    """Why moving src's accounts into dst is not allowed, or None."""
    if src == dst:
        return "Es la misma carpeta."
    if is_inside(dst, app_dir()):
        return "La carpeta de descargas no puede estar dentro de los datos privados de Bakin."
    for acc in accounts(src):
        if dst == acc or is_inside(dst, acc):
            return f"La carpeta nueva está dentro de la del perfil {acc.name}."
    return None


@dataclass
class MoveResult:
    moved: list[str] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)   # {"folder", "reason"}
    bytes: int = 0


def move_accounts(src: Path, dst: Path, folders: list[str] | None = None,
                  progress: Callable[[dict], None] = lambda p: None) -> MoveResult:
    """Move the account folders of src (all, or just `folders`) into dst."""
    todo = [d for d in accounts(src) if folders is None or d.name in folders]
    sizes = {d.name: folder_size(d)[1] for d in todo}
    total = sum(sizes.values())
    result = MoveResult()
    dst.mkdir(parents=True, exist_ok=True)
    done = 0
    for i, acc in enumerate(todo):
        target = dst / acc.name
        base = {"index": i + 1, "count": len(todo), "folder": acc.name, "total": total}
        progress({**base, "done": done})
        if target.exists():
            result.skipped.append({"folder": acc.name, "reason": "ya existe una carpeta con ese nombre en el destino"})
            done += sizes[acc.name]
            continue
        try:
            try:
                os.rename(acc, target)          # same drive: instant
            except OSError as exc:
                if exc.errno != errno.EXDEV and getattr(exc, "winerror", None) != 17:
                    raise
                _copy_verified(acc, target, lambda n: progress({**base, "done": done + n}))
                shutil.rmtree(acc)  # only after every byte was verified
        except Exception as exc:  # disk full, permissions...: the original is intact
            result.skipped.append({"folder": acc.name, "reason": f"{exc.__class__.__name__}: {exc}"})
        else:
            result.moved.append(acc.name)
            result.bytes += sizes[acc.name]
        done += sizes[acc.name]
        progress({**base, "done": done})
    return result


def _copy_verified(src: Path, final: Path, progress: Callable[[int], None]) -> None:
    staging = final.with_name(final.name + STAGING)
    if staging.exists():
        shutil.rmtree(staging)  # left by an earlier interrupted copy
    copied = 0
    try:
        for base, dirs, names in os.walk(src):
            dirs[:] = [x for x in dirs if x not in SKIP_NAMES]
            rel = Path(base).relative_to(src)
            (staging / rel).mkdir(parents=True, exist_ok=True)
            for n in names:
                if n in SKIP_NAMES:
                    continue
                a, b = Path(base) / n, staging / rel / n
                digest = _copy_file(a, b)
                if _sha256(b) != digest:
                    raise OSError(f"la copia de {rel / n} no coincide con el original")
                shutil.copystat(a, b)
                copied += b.stat().st_size
                progress(copied)
        os.rename(staging, final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _copy_file(a: Path, b: Path) -> str:
    h = hashlib.sha256()
    with open(a, "rb") as fa, open(b, "wb") as fb:
        for chunk in iter(lambda: fa.read(CHUNK), b""):
            h.update(chunk)
            fb.write(chunk)
        fb.flush()
        os.fsync(fb.fileno())
    return h.hexdigest()


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def new_record(src: Path, dst: Path, moved: list[str]) -> dict:
    return {"from": str(src), "to": str(dst), "folders": moved,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
