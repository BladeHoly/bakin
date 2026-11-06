"""The same song on YouTube Music (PLAN 0.15.2): a score from 0 to 100 for a catalogue result
against a track (title, artists, length, version, explicit). Ideas from spotDL's matcher (MIT):
version words on one side only cost, a length far off rejects, album audio beats a video.
"""

from __future__ import annotations

import math
import re
import unicodedata
from difflib import SequenceMatcher

VERSION = ("remix", "live", "en vivo", "acoustic", "acustico", "instrumental", "karaoke", "cover", "slowed",
           "sped up", "speed up", "nightcore", "8d", "remaster", "demo", "edit", "extended", "reverb")
SURE, DOUBT = 80, 62


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().casefold()
    s = re.sub(r"\((?:feat|ft|with)\.?[^)]*\)|\[(?:feat|ft)\.?[^\]]*\]|\s(?:feat|ft)\.?\s.*$", " ", s)
    s = re.sub(r"[\(\)\[\]]", " ", s)
    s = re.sub(r"\s-\s(?:\d{4}\s)?remaster(?:ed)?(?:\s\d{4})?", " remaster", s)
    return " ".join(re.sub(r"[^\w\s]", " ", s).split())


def _versions(s: str) -> set[str]:
    n = f" {_norm(s)} "
    return {v for v in VERSION if f" {v} " in n}


def _ratio(a: str, b: str) -> float:
    """Like rapidfuzz's token_set_ratio: the shared words against each side (0–100)."""
    ta, tb = set(_norm(a).split()), set(_norm(b).split())
    if not ta or not tb:
        return 0.0
    common = " ".join(sorted(ta & tb))
    ra, rb = " ".join(sorted(ta)), " ".join(sorted(tb))
    scores = [SequenceMatcher(None, ra, rb).ratio()]
    if common:
        scores += [SequenceMatcher(None, common, ra).ratio(), SequenceMatcher(None, common, rb).ratio()]
    return 100 * max(scores)


def _artist_score(want: list[str], got: str) -> float:
    if not want:
        return 70.0
    got_n = _norm(got)
    main = _norm(want[0])
    if main and main in got_n:
        return 100.0
    return max(_ratio(a, got) for a in want)


def score(track: dict, card: dict) -> dict:
    """{"score", "verdict": sure | doubt | no, "why"} for a catalogue song card."""
    title = _ratio(track["title"], card.get("title", ""))
    artist = _artist_score(track.get("artists") or [], card.get("artist", ""))
    why = []
    d_track, d_card = track.get("duration"), card.get("duration")
    if d_track and d_card:
        delta = abs(float(d_track) - float(d_card))
        timing = 100 * math.exp(-0.1 * delta)
        if delta > max(15, 0.07 * float(d_track)):
            return {"score": 0, "verdict": "no", "why": f"dura {int(delta)} s distinto"}
    else:
        timing = 60.0
    s = 0.45 * title + 0.35 * artist + 0.20 * timing
    if card.get("official"):
        s += 5
    mine, theirs = _versions(track["title"]), _versions(card.get("title", ""))
    extra = mine ^ theirs
    if extra:
        s -= 15 * len(extra)
        why.append("otra versión: " + ", ".join(sorted(extra)))
    if track.get("explicit") != card.get("explicit") and card.get("explicit") is not None:
        s -= 5
    if title < 60 or artist < 60:
        return {"score": round(max(0, min(s, 59))), "verdict": "no", "why": "otro tema"}
    s = round(max(0, min(100, s)))
    verdict = "sure" if s >= SURE else "doubt" if s >= DOUBT else "no"
    if extra and verdict == "sure":  # another version is never taken without a look
        verdict = "doubt"
    return {"score": s, "verdict": verdict, "why": "; ".join(why)}


def query(track: dict) -> str:
    return f"{', '.join(track.get('artists') or [])} - {track['title']}"


def best(track: dict, cards: list[dict]) -> dict:
    """{"match": card or None, "score", "verdict", "why", "alternatives": [up to 4 cards]}."""
    ranked = sorted(((score(track, c), c) for c in cards if c.get("kind") == "song" and c.get("available", True)),
                    key=lambda x: -x[0]["score"])
    if not ranked or ranked[0][0]["verdict"] == "no":
        return {"match": None, "score": ranked[0][0]["score"] if ranked else 0, "verdict": "no",
                "why": "No encontrado en YouTube Music", "alternatives": [c for _, c in ranked[:4]]}
    top, card = ranked[0]
    return {"match": card, **top, "alternatives": [c for _, c in ranked[1:5]]}
