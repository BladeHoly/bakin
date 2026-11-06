"""Tag changes the user confirmed (PLAN 0.14.1–0.14.2): «Limpiar títulos…» and MusicBrainz.

Each apply is a batch: the previous tags (and cover, if it changes) of every song go to
`library/tags-backup.jsonl` (covers as files beside it) before the file is written, so «Deshacer»
puts them back. Nothing is written without a change the user ticked.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

from . import audiotags
from .paths import app_dir


def _backup_file() -> Path:
    return app_dir() / "library" / "tags-backup.jsonl"


def _covers_dir() -> Path:
    return app_dir() / "library" / "tags-backup-covers"


def apply(changes: list[dict], skip_paths: set[str] = frozenset()) -> dict:
    """`changes`: [{"id", "path", "title"?, "artist"?, "album"?, "year"?, "cover"?: bytes}].
    Songs in `skip_paths` (the one playing: Windows keeps it open) are left for later."""
    batch = uuid.uuid4().hex[:12]
    done, failed, skipped = [], [], []
    backup = _backup_file()
    backup.parent.mkdir(parents=True, exist_ok=True)
    for ch in changes:
        path = Path(ch["path"])
        if str(path) in skip_paths:
            skipped.append(ch["id"])
            continue
        fields = {k: ch[k] for k in ("title", "artist", "album", "year") if ch.get(k) is not None}
        try:
            before = audiotags.snapshot(path)
            cover_file = None
            if ch.get("cover"):
                old = audiotags.cover(path)
                if old:
                    _covers_dir().mkdir(parents=True, exist_ok=True)
                    cover_file = _covers_dir() / f"{hashlib.sha1(old).hexdigest()}.img"
                    if not cover_file.exists():
                        cover_file.write_bytes(old)
            with open(backup, "a", encoding="utf-8") as fp:
                fp.write(json.dumps({"batch": batch, "at": time.time(), "id": ch["id"], "path": str(path),
                                     "before": before, "cover": str(cover_file) if cover_file else None,
                                     "cover_changed": bool(ch.get("cover"))}, ensure_ascii=False) + "\n")
            audiotags.write(path, **fields)
            if ch.get("cover"):
                audiotags.write_cover(path, ch["cover"])
            audiotags.forget(path)
            done.append(ch["id"])
        except (OSError, ValueError) as exc:
            failed.append({"id": ch["id"], "error": _why(exc)})
    return {"batch": batch if done else None, "done": done, "failed": failed, "skipped": skipped}


def _why(exc: Exception) -> str:
    if isinstance(exc, PermissionError):
        return "El archivo está en uso (¿sonando?): se cambia la próxima vez."
    return str(exc) or exc.__class__.__name__


def _records() -> list[dict]:
    try:
        lines = _backup_file().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def last_batch() -> dict | None:
    """{"batch", "at", "count"} of the last change that can be undone."""
    recs = [r for r in _records() if not r.get("undone")]
    if not recs:
        return None
    b = recs[-1]["batch"]
    mine = [r for r in recs if r["batch"] == b]
    return {"batch": b, "at": mine[-1]["at"], "count": len(mine)}


def undo(batch: str | None = None) -> dict:
    """Puts back the tags (and covers) a batch changed; the last one by default."""
    recs = _records()
    batch = batch or (last_batch() or {}).get("batch")
    mine = [r for r in recs if r["batch"] == batch and not r.get("undone")]
    done, failed = [], []
    for r in reversed(mine):
        path = Path(r["path"])
        try:
            audiotags.write(path, **{k: r["before"].get(k, "") for k in ("title", "artist", "album", "year")})
            if r.get("cover_changed"):
                old = Path(r["cover"]).read_bytes() if r.get("cover") else None
                audiotags.write_cover(path, old)
            audiotags.forget(path)
            r["undone"] = True
            done.append(r["id"])
        except (OSError, ValueError) as exc:
            failed.append({"id": r["id"], "error": _why(exc)})
    if done:
        tmp = _backup_file().with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")
        tmp.replace(_backup_file())
    return {"done": done, "failed": failed}
