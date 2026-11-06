"""Bakin Música: play counts and the player's state.

Both live in Bakin's private data folder (music/plays.json, music/player.json)
and hold only library ids, numbers and the player's settings. The player
itself runs in the window (the audio element); the engine is only asked
when a track changes or a song counts as played.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

from .paths import app_dir

MAX_QUEUE = 20000
REPEAT = ("off", "all", "one")

_lock = threading.Lock()
_plays: dict | None = None


def _file(name: str) -> Path:
    return app_dir() / "music" / name


def _read(name: str) -> dict:
    try:
        data = json.loads(_file(name).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(name: str, data: dict) -> None:
    p = _file(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=p.stem, suffix=".tmp", dir=p.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False)
    os.replace(tmp, p)


def reset_memory_cache() -> None:  # tests
    global _plays
    with _lock:
        _plays = None


def _log_play(item_id: str, at: float) -> None:
    """Every play with its time (music/plays-log.jsonl, PLAN 0.14.4–0.14.5): the hour of day for
    the mixes and the statistics; plays.json keeps only the totals."""
    p = _file("plays-log.jsonl")
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fp:
        fp.write(json.dumps({"id": item_id, "at": round(at, 1)}) + "\n")


def play_log(since: float = 0) -> list[tuple[str, float]]:
    """(id, unix time) of each play logged (from 0.14 on), oldest first."""
    out = []
    try:
        lines = _file("plays-log.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            rec = json.loads(line)
            at = float(rec["at"])
        except (ValueError, KeyError, TypeError):
            continue
        if at >= since:
            out.append((str(rec["id"]), at))
    return out


def skips() -> dict:
    """{item id: {"n", "last"}}: songs passed over with «next» before 30 s (the shell writes them
    to music/skips-pending.jsonl; they're added up here)."""
    with _lock:
        data = {k: v for k, v in _read("skips.json").items() if isinstance(v, dict)}
        path = _file("skips-pending.jsonl")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return data
        for line in lines:
            try:
                rec = json.loads(line)
                item_id, at = str(rec["id"]), float(rec.get("at") or 0)
            except (ValueError, KeyError, TypeError):
                continue
            e = data.setdefault(item_id, {"n": 0, "last": 0})
            e["n"] = int(e.get("n", 0)) + 1
            e["last"] = max(float(e.get("last", 0) or 0), at)
        _write("skips.json", data)
        try:
            path.unlink()
        except OSError:
            pass
        return data


# ------------------------------------------------------------------ plays

def plays() -> dict:
    """{item id: {"n": times played, "last": unix time}}."""
    global _plays
    with _lock:
        if _plays is None:
            _plays = {k: v for k, v in _read("plays.json").items() if isinstance(v, dict)}
            if _merge_pending(_plays):
                _write("plays.json", dict(_plays))
        return _plays


def _merge_pending(data: dict) -> bool:
    """Plays the shell counted while the engine slept (music/plays-pending.jsonl,
    one {"id", "at"} per line): add them, then empty the file."""
    path = _file("plays-pending.jsonl")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    changed = False
    for line in lines:
        try:
            rec = json.loads(line)
            item_id, at = str(rec["id"]), float(rec.get("at") or 0)
        except (ValueError, KeyError, TypeError):
            continue
        entry = data.setdefault(item_id, {"n": 0, "last": 0})
        entry["n"] = int(entry.get("n", 0)) + 1
        entry["last"] = max(float(entry.get("last", 0) or 0), at)
        if at:
            _log_play(item_id, at)
        changed = True
    try:
        path.unlink()
    except OSError:
        pass
    return changed


def merge_pending_plays() -> None:
    """At start: plays counted by the shell while the engine slept."""
    global _plays
    with _lock:
        _plays = None
    plays()


def record_play(item_id: str) -> dict:
    """One more play of a song (the window decides when it counts: half the
    song or four minutes, whichever comes first)."""
    item_id = str(item_id)
    data = plays()
    with _lock:
        entry = data.setdefault(item_id, {"n": 0, "last": 0})
        entry["n"] = int(entry.get("n", 0)) + 1
        entry["last"] = time.time()
        snapshot = dict(data)
    _write("plays.json", snapshot)
    _log_play(item_id, entry["last"])
    return entry


def move_plays(old_id: str, new_id: str) -> None:
    """A song replaced by a better copy keeps its history."""
    data = plays()
    with _lock:
        if old_id not in data:
            return
        old = data.pop(old_id)
        cur = data.setdefault(new_id, {"n": 0, "last": 0})
        cur["n"] = int(cur.get("n", 0)) + int(old.get("n", 0))
        cur["last"] = max(cur.get("last", 0), old.get("last", 0))
        snapshot = dict(data)
    _write("plays.json", snapshot)


# ------------------------------------------------------------------ state

def _num(v, lo, hi, default):
    try:
        return min(hi, max(lo, float(v)))
    except (TypeError, ValueError):
        return default


def clean_state(raw: dict | None) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    ids = [str(i) for i in (raw.get("ids") or []) if isinstance(i, str) and i][:MAX_QUEUE]
    order = raw.get("order")
    if not (isinstance(order, list) and sorted(order) == list(range(len(ids)))):
        order = list(range(len(ids)))
    return {
        "ids": ids,
        "order": order,                                    # play order (shuffled or not)
        "pos": int(_num(raw.get("pos"), 0, max(0, len(ids) - 1), 0)),
        "time": _num(raw.get("time"), 0, 24 * 3600, 0),     # seconds into the current song
        "shuffle": bool(raw.get("shuffle")),
        "repeat": raw.get("repeat") if raw.get("repeat") in REPEAT else "off",
        "volume": _num(raw.get("volume"), 0, 1, 0.8),
        "muted": bool(raw.get("muted")),
        "crossfade": int(_num(raw.get("crossfade"), 0, 12, 0)),
        "speed": _num(raw.get("speed"), 0.5, 2, 1),
        "source": str(raw.get("source") or "")[:120],      # what the queue was started from
    }


def load_state() -> dict:
    return clean_state(_read("player.json"))


def save_state(raw: dict) -> dict:
    state = clean_state(raw)
    _write("player.json", state)
    return state


# -------------------------------------------------------------- playlists
# Normal playlists hold library ids in order; smart ones hold rules and are
# filled from the library each time they're opened.

FIELDS = {  # rule field -> kind of value
    "title": "text", "artist": "text", "album": "text", "name": "text",
    "plays": "number", "bitrate": "number", "minutes": "number",
    "added_days": "days", "played_days": "days",
    "fav": "bool", "codec": "text",
}
OPS = {"text": ("contains", "not_contains", "is"), "number": ("gte", "lte"),
       "days": ("within", "not_within"), "bool": ("is",)}
SORTS = ("new", "plays", "last_played", "title", "artist", "random")


def clean_rules(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    rules = []
    for r in (raw.get("rules") or [])[:20]:
        if not isinstance(r, dict) or r.get("field") not in FIELDS:
            continue
        kind = FIELDS[r["field"]]
        if r.get("op") not in OPS[kind]:
            continue
        v = r.get("value")
        if kind == "text":
            v = str(v or "").strip()[:100]
            if not v:
                continue
        elif kind == "bool":
            v = bool(v)
        else:
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
        rules.append({"field": r["field"], "op": r["op"], "value": v})
    try:
        limit = max(0, min(5000, int(raw.get("limit") or 0)))
    except (TypeError, ValueError):
        limit = 0
    return {"match": "any" if raw.get("match") == "any" else "all", "rules": rules,
            "sort": raw.get("sort") if raw.get("sort") in SORTS else "new", "limit": limit}


def _rule_ok(it: dict, r: dict, now: float) -> bool:
    f, op, v = r["field"], r["op"], r["value"]
    kind = FIELDS[f]
    if kind == "text":
        have = str(it.get(f) or "").lower()
        want = v.lower()
        return {"contains": want in have, "not_contains": want not in have, "is": have == want}[op]
    if kind == "bool":
        return bool(it.get(f)) == v
    if kind == "days":
        stamp = it.get("date") if f == "added_days" else it.get("last_played")
        recent = bool(stamp) and now - stamp <= v * 86400
        return recent if op == "within" else not recent
    have = {"plays": it.get("plays") or 0, "bitrate": it.get("bitrate") or 0,
            "minutes": (it.get("duration") or 0) / 60}[f]
    return have >= v if op == "gte" else have <= v


def sort_items(items: list[dict], sort: str, seed: str = "") -> list[dict]:
    import random
    if sort == "plays":
        return sorted(items, key=lambda i: (-(i.get("plays") or 0), -(i.get("last_played") or 0)))
    if sort == "last_played":
        return sorted(items, key=lambda i: -(i.get("last_played") or 0))
    if sort in ("title", "artist"):
        return sorted(items, key=lambda i: (not i.get(sort), str(i.get(sort) or i["name"]).casefold()))
    if sort == "random":
        out = list(items)
        random.Random(seed).shuffle(out)
        return out
    return sorted(items, key=lambda i: (-i["date"], i["rel"]))


def apply_rules(items: list[dict], rules: dict, now: float | None = None) -> list[dict]:
    rules = clean_rules(rules)
    now = now or time.time()
    test = all if rules["match"] == "all" else any
    out = [it for it in items if not rules["rules"] or test(_rule_ok(it, r, now) for r in rules["rules"])]
    out = sort_items(out, rules["sort"], seed=time.strftime("%Y-%m-%d"))
    return out[:rules["limit"]] if rules["limit"] else out


def playlists() -> list[dict]:
    data = _read("playlists.json").get("playlists")
    return [p for p in (data or []) if isinstance(p, dict) and p.get("id") and p.get("name")]


def _save_playlists(items: list[dict]) -> None:
    _write("playlists.json", {"playlists": items})


def playlist(pid: str) -> dict | None:
    return next((p for p in playlists() if p["id"] == pid), None)


def playlist_save(pid: str | None, name: str, kind: str = "list", rules=None, ids=None) -> dict:
    import uuid
    name = str(name or "").strip()[:80]
    if not name:
        raise ValueError("Ponle un nombre a la playlist.")
    items = playlists()
    cur = next((p for p in items if p["id"] == pid), None) if pid else None
    if cur is None:
        cur = {"id": uuid.uuid4().hex[:10], "kind": "smart" if kind == "smart" else "list", "ids": [],
               "created": time.time()}
        items.append(cur)
    cur["name"] = name
    cur["updated"] = time.time()  # a backup's newer copy wins on import (PLAN 0.18.1)
    if cur["kind"] == "smart":
        cur["rules"] = clean_rules(rules)
        cur.pop("ids", None)
    elif ids is not None:
        cur["ids"] = [str(i) for i in ids][:MAX_QUEUE]
    _save_playlists(items)
    return cur


def playlist_add(pid: str, ids: list) -> int:
    items = playlists()
    cur = next((p for p in items if p["id"] == pid and p["kind"] == "list"), None)
    if cur is None:
        raise ValueError("Esa playlist ya no existe.")
    have = set(cur["ids"])
    new = []
    for i in (str(x) for x in ids):
        if i not in have:  # once each, also within this call
            have.add(i)
            new.append(i)
    cur["ids"] = (cur["ids"] + new)[:MAX_QUEUE]
    _save_playlists(items)
    return len(new)


def playlist_remove(pid: str, ids: list) -> int:
    items = playlists()
    cur = next((p for p in items if p["id"] == pid and p["kind"] == "list"), None)
    if cur is None:
        return 0
    drop = {str(i) for i in ids}
    before = len(cur["ids"])
    cur["ids"] = [i for i in cur["ids"] if i not in drop]
    _save_playlists(items)
    return before - len(cur["ids"])


def playlist_mirror(pid: str, conf: dict | None) -> bool:
    """A playlist's mirror folder (PLAN 0.20.1): {"folder", "mode", "remove_ok", "adopt"}, or None
    to stop mirroring."""
    items = playlists()
    cur = next((p for p in items if p["id"] == pid), None)
    if cur is None:
        return False
    if conf:
        cur["mirror"] = conf
    else:
        cur.pop("mirror", None)
    _save_playlists(items)
    return True


def playlist_delete(pid: str) -> bool:
    items = playlists()
    keep = [p for p in items if p["id"] != pid]
    _save_playlists(keep)
    return len(keep) < len(items)


def rename_ids(old_id: str, new_id: str) -> None:
    """A song replaced by a better copy keeps its place in playlists."""
    items = playlists()
    changed = False
    for p in items:
        if p.get("kind") == "list" and old_id in p.get("ids", []):
            p["ids"] = [new_id if i == old_id else i for i in p["ids"]]
            changed = True
    if changed:
        _save_playlists(items)


def m3u8(items: list[dict]) -> str:
    """An .m3u8 playlist (absolute paths) other players and phones can read."""
    lines = ["#EXTM3U"]
    for it in items:
        who = " - ".join(x for x in (it.get("artist"), it.get("title")) if x) or it["name"]
        lines.append(f"#EXTINF:{int(it.get('duration') or -1)},{who}")
        lines.append(it["path"])
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ mixes
# Made from the user's own library and play history, the same all day
# (seeded with the date), so a mix doesn't change each time it's opened.

def _weighted(items: list[dict], k: int, rng, weight) -> list[dict]:
    pool = list(items)
    out = []
    while pool and len(out) < k:
        total = sum(weight(i) for i in pool)
        x = rng.uniform(0, total)
        for n, it in enumerate(pool):
            x -= weight(it)
            if x <= 0:
                out.append(pool.pop(n))
                break
        else:
            out.append(pool.pop())
    return out


def _bucket(at: float) -> str:
    """Morning (5–12 h), afternoon (12–19 h) or night."""
    h = time.localtime(at).tm_hour
    return "morning" if 5 <= h < 12 else "afternoon" if 12 <= h < 19 else "night"


def main_artist(artist: str) -> str:
    """«A, B» / «A & B» / «A feat. B» → «a» (for grouping)."""
    a = (artist or "").casefold()
    for sep in (",", " & ", " feat", " ft.", " x ", ";"):
        a = a.split(sep)[0]
    return a.strip()


def _spread(chosen: list[dict]) -> list[dict]:
    """The same artist twice in a row only when nothing else is left."""
    out, rest = [], list(chosen)
    while rest:
        last = main_artist(out[-1].get("artist", "")) if out else None
        k = next((n for n, i in enumerate(rest) if not last or main_artist(i.get("artist", "")) != last), 0)
        out.append(rest.pop(k))
    return out


FEW_PLAYS = 50  # under this many plays in all, the daily mix goes by what's new and loved, not by plays


ENERGY_MIN = 20  # songs measured before the «Tranquilo» / «Intenso» mixes show up


def mixes(items: list[dict], now: float | None = None, log: list | None = None,
          skipped: dict | None = None, measured: dict | None = None) -> list[dict]:
    """[{id, name, desc, ids}] for the mixes worth showing (5+ songs). They learn (PLAN 0.14.4): a
    song skipped often weighs less, what's played at this time of day weighs more, and with few
    plays the daily mix leans on what was added lately and the favourites."""
    import random
    now = now or time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    log = play_log(now - 90 * 86400) if log is None else log
    skipped = skips() if skipped is None else skipped
    out = []

    def add(mid, name, desc, chosen):
        if len(chosen) >= 5:
            out.append({"id": mid, "name": name, "desc": desc, "ids": [i["id"] for i in chosen]})

    def skip_n(i):
        return int((skipped.get(i["id"]) or {}).get("n", 0))

    now_bucket = _bucket(now)
    at_this_hour: dict[str, int] = {}
    for item_id, at in log:
        if _bucket(at) == now_bucket:
            at_this_hour[item_id] = at_this_hour.get(item_id, 0) + 1

    rng = random.Random(day + "daily")
    if sum(i.get("plays") or 0 for i in items) < FEW_PLAYS:
        newest = {i["id"] for i in sorted(items, key=lambda i: -(i.get("date") or 0))[:100]}

        def weight(i):
            return (1 + 3 * bool(i.get("fav")) + 2 * (i["id"] in newest)
                    + 2 * at_this_hour.get(i["id"], 0)) / (1 + skip_n(i))
        add("daily", "Mix del día", "Lo nuevo y tus favoritas, mezclados. Aprende de lo que escuchas y saltas.",
            _spread(_weighted(items, min(len(items), 50), rng, weight)))
    else:
        loved = [i for i in items if (i.get("plays") or 0) > 0 or i.get("fav")]
        loved_ids = {i["id"] for i in loved}
        rest = [i for i in items if i["id"] not in loved_ids and skip_n(i) < 3]

        def weight(i):
            return (1 + (i.get("plays") or 0) + 4 * bool(i.get("fav"))
                    + 3 * at_this_hour.get(i["id"], 0)) / (1 + skip_n(i))
        chosen = _weighted(loved, min(len(loved), 35), rng, weight)
        chosen += rng.sample(rest, min(len(rest), 50 - len(chosen)))
        rng.shuffle(chosen)
        add("daily", "Mix del día", "Lo que más escuchas a esta hora, con temas que hace rato no suenan. Cambia cada día.",
            _spread(chosen))

    month = 30 * 86400
    forgotten = [i for i in items if ((i.get("plays") or 0) >= 2 or i.get("fav"))
                 and (not i.get("last_played") or now - i["last_played"] > month)]
    rng = random.Random(day + "forgotten")
    add("forgotten", "Favoritas olvidadas", "Temas que te gustaban y no escuchas hace más de un mes.",
        rng.sample(forgotten, min(len(forgotten), 50)))

    fresh = [i for i in items if (i.get("plays") or 0) <= 1 and skip_n(i) < 2]
    rng = random.Random(day + "rediscover")
    add("rediscover", "Para redescubrir", "Temas de tu biblioteca que casi no escuchaste.",
        rng.sample(fresh, min(len(fresh), 50)))

    by_artist: dict[str, int] = {}
    for i in items:
        if i.get("artist") and i.get("plays"):
            by_artist[i["artist"]] = by_artist.get(i["artist"], 0) + i["plays"]
    top = sorted((a for a, n in by_artist.items() if n >= 3), key=lambda a: -by_artist[a])[:3]
    for artist in top:
        rng = random.Random(day + "radio" + artist)
        own = [i for i in items if i.get("artist") == artist]
        near = [i for i in items if i.get("artist") != artist
                and (i.get("artist") in set(top) or (i.get("plays") or 0) >= 2)]
        chosen = rng.sample(own, min(len(own), 25)) + rng.sample(near, min(len(near), 25))
        rng.shuffle(chosen)
        add(f"radio:{artist}", f"Radio de {artist}", f"{artist} y lo que más escuchas junto a sus temas.", chosen)

    # by how it sounds (PLAN 0.20.3): the loudness scan's numbers, a calm ↔ intense axis
    from . import loudness
    measured = loudness.load() if measured is None else measured
    scored = [(e, i) for i in items if skip_n(i) < 3
              for e in [loudness.energy(measured.get(i["id"]))] if e is not None]
    if len(scored) >= ENERGY_MIN:
        scored.sort(key=lambda x: x[0])
        third = max(5, len(scored) // 3)
        for mid, name, desc, part in (
                ("calm", "Tranquilo", "Lo más suave de tu biblioteca, por cómo suena.", scored[:third]),
                ("intense", "Intenso", "Lo más enérgico de tu biblioteca, por cómo suena.", scored[-third:])):
            rng = random.Random(day + mid)
            pool = [i for _, i in part]
            add(mid, name, desc, _spread(rng.sample(pool, min(len(pool), 50))))
    return out


def radio(items: list[dict], seed_id: str, now: float | None = None, log: list | None = None) -> list[dict]:
    """«Radio desde este tema» (PLAN 0.14.4): the song, more of its artist, the artists played
    close to it (within an hour of one of its artist's songs), then favourites. Up to 50 songs."""
    import random
    now = now or time.time()
    by_id = {i["id"]: i for i in items}
    seed = by_id.get(seed_id)
    if seed is None:
        return []
    log = play_log(now - 365 * 86400) if log is None else log
    artist = main_artist(seed.get("artist", ""))
    near: dict[str, int] = {}
    if artist:
        times = [at for item_id, at in log if main_artist((by_id.get(item_id) or {}).get("artist", "")) == artist]
        for item_id, at in log:
            other = main_artist((by_id.get(item_id) or {}).get("artist", ""))
            if other and other != artist and any(abs(at - t) < 3600 for t in times):
                near[other] = near.get(other, 0) + 1
    rng = random.Random(f"{seed_id}{time.strftime('%Y-%m-%d', time.localtime(now))}")
    own = [i for i in items if i["id"] != seed_id and artist and main_artist(i.get("artist", "")) == artist]
    close = [i for i in items if main_artist(i.get("artist", "")) in near]
    loved = [i for i in items if i.get("fav") or (i.get("plays") or 0) >= 2]
    chosen, seen = [seed], {seed_id}
    for pool, k in ((own, 15), (close, 25), (loved, 50), (items, 50)):
        pick = [i for i in pool if i["id"] not in seen]

        def weight(i, pool=pool):
            return (1 + near.get(main_artist(i.get("artist", "")), 0)) if pool is close else 1
        for i in _weighted(pick, max(0, min(len(pick), k, 50 - len(chosen))), rng, weight):
            chosen.append(i)
            seen.add(i["id"])
        if len(chosen) >= 50:
            break
    return [chosen[0]] + _spread(chosen[1:])
