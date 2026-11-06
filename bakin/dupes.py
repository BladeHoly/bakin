"""Duplicate songs in the music library (without acoustic fingerprints).

Two library items are the same song when their normalized title and main
artist match and their durations are within DURATION_TOLERANCE seconds.
"Normalized" drops what YouTube adds to a title ((Official Video), [4K],
(Lyrics), feat. parts, accents, case, punctuation) but keeps the words that
make a different recording (remix, live, acoustic...), so "Song" and
"Song (Remix)" are never duplicates.

Inside each group the copies are ordered by quality_rank: the format tier
decides first (lossless > Opus > AAC/Vorbis > MP3 > other) regardless of
bitrate, so a 160 kbps Opus beats a 320 kbps MP3 (an MP3 from YouTube is
always a re-encode of the Opus it serves); then bitrate, size and date.

Pure functions over the item dicts the library already builds; nothing is
read from or written to disk here.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict

DURATION_TOLERANCE = 2.0  # seconds

LOSSLESS = {"flac", "alac", "pcm", "wav", "aiff"}
CODEC_TIER = {"opus": 3, "aac": 2, "vorbis": 2, "mp3": 1}

# Words that make a different recording: kept when they appear in brackets.
VERSION_WORDS = re.compile(
    r"\b(remix|rmx|live|en vivo|acoustic|acustico|acustica|instrumental|slowed|reverb|"
    r"sped up|speed up|nightcore|cover|edit|extended|demo|karaoke|8d|mashup|bootleg)\b")
# Words that only describe the upload: a bracket with these and no version word goes.
NOISE_WORDS = re.compile(
    r"\b(official|oficial|video|videoclip|clip|audio|lyrics?|letra|visuali[sz]er|hd|hq|4k|"
    r"1080p|720p|remaster(ed)?|remasterizado|explicit|clean|mv|m v|music video|color coded|"
    r"subtitulado|sub espanol|mono|stereo|(19|20)\d\d)\b")
FEAT = re.compile(r"\b(feat|ft|featuring)\b\.?.*$")
FEAT_UNTIL_DASH = re.compile(r"\b(?:feat|ft|featuring)\b\.?.*?(?=\s-\s|$)")
BRACKET = re.compile(r"[(\[{（【]([^)\]}）】]*)[)\]}）】]")
PUNCT = re.compile(r"[\W_]+")
ID_SUFFIX = re.compile(r"\s*\[[A-Za-z0-9_-]{11}\]\s*$")
ARTIST_SPLIT = re.compile(r"\s*(?:,|;|&|\bfeat\b\.?|\bft\b\.?|\bfeaturing\b)\s*")
TOPIC = re.compile(r"\s+-\s+topic$")


def _fold(text: str) -> str:
    """Casefold and strip accents (é → e, ñ → n)."""
    s = unicodedata.normalize("NFKD", str(text or "").casefold())
    return "".join(c for c in s if not unicodedata.combining(c))


def _clean_bracket(inner: str) -> str:
    inner = FEAT.sub("", inner.strip())
    words = PUNCT.sub(" ", inner).strip()
    if not words:
        return " "
    versions = VERSION_WORDS.findall(words)
    if versions:
        return " " + " ".join(versions) + " "
    if NOISE_WORDS.search(words):
        return " "
    return " " + inner + " "  # unknown content ("Part 2", "Taylor's Version"): keep


def normalize(text: str) -> str:
    """A title or artist reduced to what identifies the recording: lowercase,
    no accents, no upload noise or feat. parts, single spaces, no punctuation."""
    s = _fold(text)
    s = BRACKET.sub(lambda m: _clean_bracket(m.group(1)), s)
    s = FEAT_UNTIL_DASH.sub(" ", s)
    # " - Remastered 2009", " - Official Video": trailing dash parts that are only noise
    parts = re.split(r"\s+-\s+", s)
    kept = [parts[0]]
    for p in parts[1:]:
        words = PUNCT.sub(" ", p).strip()
        if VERSION_WORDS.search(words):
            kept.append(" ".join(VERSION_WORDS.findall(words)))
        elif not words or NOISE_WORDS.search(words):
            continue
        else:
            kept.append(p)
    s = " ".join(kept)
    return " ".join(PUNCT.sub(" ", s).split())


def _strict(text: str) -> str:
    """Only case, accents and punctuation folded: used when a duration is missing."""
    return " ".join(PUNCT.sub(" ", _fold(text)).split())


def _from_name(name: str) -> tuple[str, str]:
    """(artist, title) from a file name like "Artist - Title [abcdefghijk].opus"."""
    stem = str(name or "")
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    stem = ID_SUFFIX.sub("", stem).strip()
    if " - " in stem:
        artist, title = stem.split(" - ", 1)
        return artist.strip(), title.strip()
    return "", stem


def _artist_title(item: dict) -> tuple[str, str]:
    title = str(item.get("title") or "").strip()
    artist = str(item.get("artist") or "").strip()
    if not title or not artist:
        fa, ft = _from_name(item.get("name") or "")
        title = title or ft
        artist = artist or fa
    return artist, title


def main_artist(artist: str) -> str:
    """The first artist of "A, B & C feat. D", normalized."""
    a = TOPIC.sub("", _fold(artist).strip())
    first = ARTIST_SPLIT.split(a, maxsplit=1)[0] if a else ""
    return normalize(first or a)


def quality_rank(item: dict) -> tuple:
    """Bigger is better: (format tier, bitrate, size, mtime). The tier wins
    regardless of bitrate: lossless 4, Opus 3, AAC/Vorbis 2, MP3 1, other 0."""
    codec = str(item.get("codec") or "").lower()
    tier = 4 if (item.get("lossless") or codec in LOSSLESS) else CODEC_TIER.get(codec, 0)

    def num(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0
    return (tier, num(item.get("bitrate")), num(item.get("size")), num(item.get("mtime")))


def _best_first(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda it: (tuple(-x for x in quality_rank(it)), str(it.get("id"))))


def _duration(item: dict) -> float | None:
    try:
        d = float(item.get("duration"))
    except (TypeError, ValueError):
        return None
    return d if d > 0 else None


def _split_by_duration(items: list[dict]) -> list[list[dict]]:
    """Clusters where every pair is within the tolerance (no chaining):
    sorted by duration, each cluster spans at most DURATION_TOLERANCE."""
    out: list[list[dict]] = []
    for it in sorted(items, key=lambda x: (_duration(x), str(x.get("id")))):
        if out and _duration(it) - _duration(out[-1][0]) <= DURATION_TOLERANCE + 1e-9:
            out[-1].append(it)
        else:
            out.append([it])
    return out


def groups(items: list[dict]) -> list[dict]:
    """Groups of 2+ items that are the same song, sorted by artist and title.
    Each: {"key", "best": id, "items": [ids, best first], "why", "sure"}."""
    buckets: dict[tuple[str, str], list[tuple[dict, str]]] = defaultdict(list)
    for it in items:
        if not isinstance(it, dict) or it.get("id") is None:
            continue
        artist, title = _artist_title(it)
        t = normalize(title)
        if not t:
            continue
        buckets[(main_artist(artist), t)].append((it, _strict(artist) + "|" + _strict(title)))

    result = []
    for (a, t), members in buckets.items():
        if len(members) < 2:
            continue
        strict = {id(it): s for it, s in members}
        timed = [it for it, _ in members if _duration(it) is not None]
        untimed = sorted((it for it, _ in members if _duration(it) is None), key=lambda x: str(x.get("id")))
        clusters = [{"items": c, "sure": True} for c in _split_by_duration(timed)]
        loose: dict[str, list[dict]] = {}
        for it in untimed:
            # without a duration the tags must match exactly (case and accents aside)
            home = next((c for c in clusters if any(strict[id(o)] == strict[id(it)] for o in c["items"])), None)
            if home is not None:
                home["items"].append(it)
                home["sure"] = False
            else:
                loose.setdefault(strict[id(it)], []).append(it)
        clusters += [{"items": v, "sure": False} for _, v in sorted(loose.items())]
        n = 0
        for c in clusters:
            if len(c["items"]) < 2:
                continue
            ordered = _best_first(c["items"])
            ids = [it.get("id") for it in ordered]
            result.append({
                "key": f"{a}|{t}" + (f"|{n}" if n else ""),
                "best": ids[0],
                "items": ids,
                "why": "Mismo título, artista y duración" if c["sure"]
                else "Mismo título y artista (sin duración para comparar)",
                "sure": c["sure"],
                "_sort": (a, t, n),
            })
            n += 1
    result.sort(key=lambda g: g["_sort"])
    for g in result:
        del g["_sort"]
    return result
