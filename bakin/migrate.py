"""Versioned data files and their migrations (PLAN 0.18.2).

Each JSON file in Bakin's private data folder may carry a top-level
``"_v": N``; a file without it is version 0. `run()` (called once when the
engine starts, before anything reads these files) brings every registered
file up to its newest version, one step at a time:

* a copy of the file as it was is kept next to it, ``<stem>.before-<old v>.json``
  (written once per version, never overwritten: the first one is the original);
* the new file is written atomically (a temporary file, then os.replace);
* a file that can't be read or parsed, isn't a JSON object, says it is from a
  newer Bakin, or whose step fails, is left exactly as it is (and logged);
* a second run changes nothing.

This doesn't change how the files are read: every reader still accepts the
old shapes and ignores ``_v``. Only files whose reader ignores an unknown
top-level key are registered.

Writers that rebuild a file from known keys drop ``_v`` (service.save_settings,
Service._save_queue / _save_follows, mediadl.save_settings); such a file is
simply stamped again on the next start (its steps are no-ops on the current
shape, and the first backup is kept). `stamp()` lets a writer keep the key.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Callable

from .paths import app_dir

log = logging.getLogger("bakin.migrate")

VERSION_KEY = "_v"
Step = Callable[[dict], dict]


# ------------------------------------------------------------------ steps
# A step takes the file's data (a dict, without "_v") and returns the next
# version's data. Steps must be idempotent on data already in the new shape:
# a writer that drops "_v" makes the file version 0 again.

def _ui_settings_1(data: dict) -> dict:
    """ui-settings.json v1: the same keys; only the version is stamped
    (service.load_settings keeps the keys it knows)."""
    return data


def _media_1(data: dict) -> dict:
    """media.json v1: every preset carries the keys a current clean_preset
    gives it, validated, so presets saved by older versions (fewer keys, or
    no id: a new random one on every load) are stored the way they're used.
    Entries that aren't objects (the loader would fail on them) are dropped."""
    from . import mediadl
    presets = data.get("presets")
    if isinstance(presets, list):
        data["presets"] = [mediadl.clean_preset(p) for p in presets if isinstance(p, dict)]
    return data


_QUEUE_KINDS = ("media", "expand", "convert", "wallhaven")
_LANES = ("media", "wallhaven", "convert")
_OLD_PAUSE_KEYS = ("paused", "scope", "reason", "paused_until")


def _queue_1(data: dict) -> dict:
    """queue.json v1: the single pause saved by 0.13 and before
    ({"paused", "scope", "reason", "paused_until"}) becomes per-lane "holds"
    (what Service._restore_queue does with it), and jobs of kinds Bakin no
    longer runs go, as the queue drops them when it loads."""
    items = data.get("items")
    if isinstance(items, list):
        data["items"] = [d for d in items if isinstance(d, dict) and d.get("kind") in _QUEUE_KINDS]
    if not isinstance(data.get("holds"), dict) and data.get("paused"):
        scope = data.get("scope")
        lanes = (scope,) if scope in _LANES else _LANES
        hold = {"until": data.get("paused_until"), "reason": str(data.get("reason") or "")}
        data["holds"] = {lane: dict(hold) for lane in lanes}
    for k in _OLD_PAUSE_KEYS:
        data.pop(k, None)
    return data


_FOLLOW_KINDS = ("media", "wallhaven")  # what Service._check_follows checks


def _follows_1(data: dict) -> dict:
    """follows.json v1: follows of kinds Bakin no longer checks (kept by older
    versions, hidden since) go."""
    follows = data.get("follows")
    if isinstance(follows, list):
        data["follows"] = [f for f in follows if not (isinstance(f, dict) and f.get("kind") not in (None, *_FOLLOW_KINDS))]
    return data


# Keyed by the file's path relative to app_dir(), with "/" separators.
# Versions start at 1 and go up by one; the last one is the file's current version.
MIGRATIONS: dict[str, list[tuple[int, Step]]] = {
    "ui-settings.json": [(1, _ui_settings_1)],
    "media.json": [(1, _media_1)],
    "queue.json": [(1, _queue_1)],
    "follows.json": [(1, _follows_1)],
}


# ------------------------------------------------------------------ framework

def target_version(rel: str) -> int:
    steps = MIGRATIONS.get(rel) or []
    return steps[-1][0] if steps else 0


def stamp(rel: str, data: dict) -> dict:
    """`data` with the file's current version, for a writer that wants to keep it."""
    v = target_version(rel)
    return {**data, VERSION_KEY: v} if v else data


def _check_registry() -> None:
    for rel, steps in MIGRATIONS.items():
        versions = [v for v, _ in steps]
        if versions != list(range(1, len(versions) + 1)):
            raise ValueError(f"migrations for {rel} must be numbered 1, 2, 3…: {versions}")


def _write_atomic(path: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix=path.stem, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def backup_path(path: Path, version: int) -> Path:
    return path.with_name(f"{path.stem}.before-{version}.json")


def migrate_file(path: Path, steps: list[tuple[int, Step]]) -> tuple[int, int] | None:
    """Bring one file up to date; (old, new) if it was rewritten, else None."""
    if not steps or not path.is_file():
        return None
    name = path.name
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        log.warning("migrate: %s left alone (can't be read: %s)", name, exc.__class__.__name__)
        return None
    if not isinstance(data, dict):
        log.warning("migrate: %s left alone (not a JSON object)", name)
        return None
    old = data.get(VERSION_KEY, 0)
    if isinstance(old, bool) or not isinstance(old, int) or old < 0:
        log.warning("migrate: %s left alone (bad version %r)", name, old)
        return None
    target = steps[-1][0]
    if old >= target:
        if old > target:
            log.info("migrate: %s is version %d, newer than this Bakin's %d: left alone", name, old, target)
        return None
    cur = {k: v for k, v in data.items() if k != VERSION_KEY}
    try:
        for v, step in steps:
            if v <= old:
                continue
            cur = step(cur)
            if not isinstance(cur, dict):
                raise TypeError(f"step {v} returned {type(cur).__name__}")
    except Exception as exc:  # a step that fails leaves the file as it was
        log.warning("migrate: %s left alone (step failed: %s: %s)", name, exc.__class__.__name__, exc)
        return None
    out = {VERSION_KEY: target, **{k: v for k, v in cur.items() if k != VERSION_KEY}}
    try:
        backup = backup_path(path, old)
        if not backup.exists():
            shutil.copy2(path, backup)
        _write_atomic(path, out)
    except OSError as exc:
        log.warning("migrate: %s not migrated (%s)", name, exc.__class__.__name__)
        return None
    log.info("migrate: %s %d -> %d", name, old, target)
    return old, target


def run(base: Path | None = None) -> dict[str, tuple[int, int]]:
    """Migrate every registered file that exists. Call once when the engine
    starts, before the files are read. Returns {relative path: (old, new)}
    for the files it rewrote."""
    _check_registry()
    base = Path(base) if base is not None else app_dir()
    changed: dict[str, tuple[int, int]] = {}
    for rel, steps in MIGRATIONS.items():
        try:
            result = migrate_file(base.joinpath(*rel.split("/")), steps)
        except Exception as exc:  # never let one file stop the engine from starting
            log.warning("migrate: %s skipped (%s)", rel, exc.__class__.__name__)
            continue
        if result:
            changed[rel] = result
    return changed
