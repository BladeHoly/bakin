"""Offline clean-up of song titles: proposes artist / title / featuring for a
library song from its tags and file name, without asking any service.

Downloads from YouTube Music already carry good tags; plain YouTube videos
come as «Artist - Song (Official Video) [id]» with the channel as artist.
`propose` turns those into tags; it never writes anything itself.
"""

from __future__ import annotations

import re
import unicodedata

# yt-dlp's trailing «[video id]» in file names (11 chars, not a plain word)
_ID_TAIL = re.compile(r"\s*\[(?=[A-Za-z0-9_-]{11}\])(?![a-z]{11}\]|[A-Z][a-z]{10}\])[A-Za-z0-9_-]{11}\]\s*$")
# Windows-safe look-alikes yt-dlp puts in file names
_NAME_CHARS = str.maketrans({"⧸": "/", "＂": '"', "｜": "|"})

_SPLIT = re.compile(r"\s+([-–—|｜])\s+")
_JP_QUOTE = re.compile(r"^(?P<a>[^「『]+?)\s*[「『](?P<t>[^」』]+)[」』]\s*(?P<rest>.*)$")
_TRACK_NO = re.compile(r"^\d{1,3}\.?$")

_NOISE_WORD = (
    r"official\s+(?:music\s+|lyrics?\s+|hd\s+|4k\s+)?(?:video|audio|visuali[sz]er|mv)"
    r"|(?:music\s+)?video\s+(?:oficial|official|lyrics?)|audio\s+oficial"
    r"|lyrics?(?:\s+video)?|letra(?:\s+oficial)?|con\s+letra"
    r"|video\s*clip(?:\s+oficial)?|(?:anime\s+)?music\s+video|with\s+lyrics|4k\s+upgrade|official\s+promo"
    r"|visuali[sz]er|official|video|audio|hd|hq|4k|1080p|720p|explicit|clean|m/?v"
    r"|remaster(?:ed)?\s+\d{4}|\d{4}\s+remaster(?:ed)?|remastered"
)
_SEQ = rf"(?:{_NOISE_WORD})(?:\s*[/,&+|-]?\s*(?:{_NOISE_WORD}))*"
_BRACKET_NOISE = re.compile(rf"\s*[(\[【]\s*(?P<n>{_SEQ})\s*[)\]】]", re.I)
_DASH_NOISE = re.compile(rf"\s+[-–—|｜]\s+(?P<n>{_SEQ})\s*$", re.I)
_EMPTY_BRACKETS = re.compile(r"\s*(?:\(\s*\)|\[\s*\]|【\s*】)")

_FEAT_BRACKET = re.compile(r"\s*[(\[]\s*(?:feat\.?|ft\.?|featuring|with)\s+(?P<who>[^)\]]+?)\s*[)\]]", re.I)
_FEAT_BARE = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+(?P<who>.+?)(?=\s*[(\[【]|\s+[-–—|｜]\s+|$)", re.I)
_FEAT_SPLIT = re.compile(r"\s*(?:,|&|\s+and\s+|\s+y\s+)\s*", re.I)

_VERSION = re.compile(r"[(\[][^)\]]*\b(?:remix|live|acoustic|ac[uú]stico|en vivo|slowed|sped up|instrumental"
                      r"|cover|edit|extended|version|versi[oó]n|mix)\b[^)\]]*[)\]]", re.I)
_CHANNEL = re.compile(r"\b(?:records|recordings|music|official|tv|channel|entertainment|lyrics)\b|vevo$", re.I)
_TOPIC = re.compile(r"\s+-\s+topic$", re.I)
_VEVO = re.compile(r"^(?P<a>.+?)\s*vevo$", re.I)

_QUOTES = "\"'“”‘’«»"
_EDGE_JUNK = re.compile(r"^[\s\-–—|｜:~·•,]+|[\s\-–—|｜:~·•,]+$")
_KEEP_UPPER = {"DJ", "MC", "EP", "LP", "TV", "UK", "USA", "NYC", "LA", "OK", "BTS", "ABBA", "AC", "DC", "MGMT", "UB", "R&B"}
_ROMAN = re.compile(r"^(?=[IVX])X{0,3}(?:IX|IV|V?I{0,3})$")
_WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*")


def _ws(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _norm(s: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", s).casefold())


def _stem(name: str) -> str:
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return _ws(_ID_TAIL.sub("", stem.translate(_NAME_CHARS)))


def _is_upper(s: str) -> bool:
    """All cased letters uppercase, in at least two real words (a lone «MV»
    in a Japanese title, or a stylized «HUMBLE.», is left alone)."""
    words = [w for w in _WORD.findall(s) if len(w) > 1 and w.upper() != w.lower()]
    return len(words) >= 2 and all(w == w.upper() for w in words)


def _title_case(s: str) -> str:
    def fix(m: re.Match) -> str:
        w = m.group(0)
        if len(w) == 1 or w in _KEEP_UPPER or _ROMAN.match(w):
            return w
        return w[0] + w[1:].lower()
    return _WORD.sub(fix, s)


def _tidy(s: str) -> str:
    s = _ws(_EMPTY_BRACKETS.sub("", s))
    for _ in range(3):
        s = _EDGE_JUNK.sub("", s)
        if len(s) > 1 and s[0] in _QUOTES and s[-1] in _QUOTES:
            s = s[1:-1].strip()
    return _ws(s)


def _strip_noise(s: str, removed: list[str]) -> str:
    def cut(m: re.Match) -> str:
        removed.append(_ws(m.group("n")))
        return ""
    return _ws(_BRACKET_NOISE.sub(cut, s))


def _strip_dash_noise(s: str, removed: list[str]) -> str:
    while True:
        m = _DASH_NOISE.search(s)
        if not m or not s[:m.start()].strip():
            return s
        removed.append(_ws(m.group("n")))
        s = s[:m.start()]


def _take_feats(s: str, feats: list[str]) -> str:
    def cut(m: re.Match) -> str:
        feats.extend(p for p in _FEAT_SPLIT.split(_ws(m.group("who"))) if p.strip())
        return ""
    s = _FEAT_BRACKET.sub(cut, s)
    return _ws(_FEAT_BARE.sub(cut, s))


def _clean_artist(a: str) -> tuple[str, str | None]:
    """Uploader noise out of an artist value: «X - Topic» → X, «XVEVO» → X; a name listed twice
    («A, B, B») once."""
    a = _ws(a)
    names = [n.strip() for n in a.split(",")]
    if len(names) > 1 and len({_norm(n) for n in names}) < len(names):
        a = ", ".join(dict.fromkeys(n for n in names if n))
        return a, "dupes"
    if _TOPIC.search(a):
        return _ws(_TOPIC.sub("", a)), "topic"
    m = _VEVO.match(a)
    if m and len(a) > 4:
        return _ws(re.sub(r"(?<=[a-z])(?=[A-Z])", " ", m.group("a"))), "vevo"
    return a, None


def _same_artist(left: str, artist: str) -> bool:
    """The left side of «X - Title» is the tagged artist: equal, its start, one of a list
    («bbno$, ironmouse»), or with/without a leading «The»."""
    def bare(x: str) -> str:
        return _norm(re.sub(r"^\s*the\s+", "", x, flags=re.I))
    l = bare(left)
    for a in [artist] + [n for n in re.split(r"\s*(?:,|&|;)\s*", artist) if n]:
        a = bare(a)
        if a and (l == a or l.startswith(a)):
            return True
    return False


def propose(item: dict) -> dict | None:
    """Proposed tags for a library song (keys: name, title, artist, album,
    optional uploader), or None when nothing would change."""
    cur_title = _ws(item.get("title"))
    cur_artist = _ws(item.get("artist"))
    cur_album = _ws(item.get("album"))
    uploader = _ws(item.get("uploader"))
    base = cur_title or _stem(str(item.get("name") or ""))
    if not base:
        return None

    why: list[str] = []
    removed: list[str] = []
    feats: list[str] = []
    tag_artist, flag = _clean_artist(cur_artist)
    if flag == "topic":
        why.append("quita «- Topic» del artista")
    elif flag == "vevo":
        why.append("quita «VEVO» del artista")
    elif flag == "dupes":
        why.append("quita artistas repetidos")
    channelish = bool(cur_artist) and flag != "topic" and (
        flag == "vevo" or bool(_CHANNEL.search(cur_artist))
        or (bool(uploader) and _norm(cur_artist) == _norm(uploader)))

    body = _take_feats(_strip_noise(base, removed), feats)
    m = _SPLIT.search(body)
    split = (body[:m.start()], body[m.end():], m.group(1)) if m else None
    # «RHINO» / «ZZ Top - Gimme All Your Lovin'»: the tag is the label or the channel, the
    # title names the artist (seen on the user's library, 2026-11-07); low, for a look
    clean = _SPLIT.search(_strip_dash_noise(body, []))
    label = bool(cur_artist and cur_title and clean and clean.group(1) not in "|｜"
                 and not _same_artist(body[:clean.start()], tag_artist)
                 and _norm(tag_artist) not in _norm(base) and not _TRACK_NO.match(body[:clean.start()].strip()))
    channelish = channelish or label
    high = True

    if cur_artist and cur_title and (not split or not channelish or _same_artist(split[0], tag_artist)):
        # rule 1: real tags (YouTube Music); only clean them
        artist, title, upper = tag_artist, body, _is_upper(cur_title)
        inside = bool(split and not _same_artist(split[0], tag_artist) and len(_norm(tag_artist)) > 2
                      and _norm(tag_artist) in _norm(split[0]) and split[2] not in "|｜")
        if inside:  # «Kanye West» / «Jay-Z & Kanye West - Song»: the title's artists are the full credit
            artist, title = _take_feats(split[0], feats), split[1]
            why.append("toma el crédito completo del título")
        elif split and _same_artist(split[0], tag_artist):
            left = _take_feats(split[0], feats)
            if flag == "vevo" and _norm(left) == _norm(tag_artist):
                artist = _tidy(left)
            title = split[1]
            why.append("quita el artista repetido del título")
        elif channelish:
            high = False
            why.append("el artista puede ser el canal")
        artist_upper = False
    else:
        # rule 2: «Artist - Title» in the title (or the file name)
        upper = artist_upper = _is_upper(base)
        if split and _TRACK_NO.match(split[0].strip()):
            body = split[1]
            why.append("quita el número de pista")
            m = _SPLIT.search(body)
            split = (body[:m.start()], body[m.end():], m.group(1)) if m else None
        jp = None if split else _JP_QUOTE.match(body)
        fallback = tag_artist or _clean_artist(uploader)[0]
        if split:
            left, right, sep = split
            left, right = _take_feats(left, feats), right
            known = _norm(fallback) if fallback else ""
            if known and _norm(right) == known and _norm(left) != known:
                artist, title = right, left
                high = False
                why.append("orden dudoso: parece «Título - Artista»")
            else:
                artist, title = left, right
                why.append("separa artista y título")
                if label:
                    high = False
                    why.append(f"«{cur_artist}» parece el sello o el canal")
                if (sep in "|｜" or _VERSION.search(left) and not _VERSION.search(right)
                        or _SPLIT.search(_strip_dash_noise(right, [])) or len(left) > 60):
                    high = False
                    why.append("separación dudosa")
        elif jp and (not jp.group("rest").strip(" -–—|")
                     or re.fullmatch(_SEQ, _strip_noise(jp.group("rest"), []).strip(" -–—|"), re.I)):
            artist, title = jp.group("a"), jp.group("t")
            why.append("separa artista y título")
        else:
            artist, title = fallback, body
            high = False
            if fallback and fallback != cur_artist:
                why.append("artista tomado del canal")

    title = _take_feats(_strip_dash_noise(title, removed), feats)
    title, artist = _tidy(title), _tidy(artist)
    if not title:
        return None
    if upper:
        title = _title_case(title)
        if artist_upper:
            artist = _title_case(artist)
        why.append("pasa de MAYÚSCULAS a mayúsculas iniciales")
    seen = {_norm(artist)}
    featuring = []
    for f in (_tidy(f) for f in feats):
        if f and _norm(f) not in seen:
            seen.add(_norm(f))
            featuring.append(_title_case(f) if upper and f == f.upper() and len(f) > 3 else f)
    if featuring:
        why.append("pasa los invitados (feat.) aparte")
    if removed:
        why.append(f"quita «{removed[0]}»" + (" y más" if len(removed) > 1 else ""))

    album = _tidy(_strip_dash_noise(_strip_noise(cur_album, []), [])) if cur_album else ""
    album_out = album if album and album != cur_album else None
    if artist == cur_artist and title == base and album_out is None and not featuring:
        return None
    if artist == cur_artist and title == cur_title and album_out is None:
        return None
    reason = "; ".join(dict.fromkeys(why)) or "limpia el título"
    return {"artist": artist, "title": title, "album": album_out, "featuring": featuring,
            "confidence": "high" if high else "low", "why": reason[0].upper() + reason[1:]}
