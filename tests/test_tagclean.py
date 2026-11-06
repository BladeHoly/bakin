"""Offline title clean-up (tagclean.propose) and tag writing with undo."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from bakin import audiotags
from bakin.tagclean import propose

ID = "[dQw4w9WgXcQ]"


def _p(name="x.opus", **tags):
    return propose({"name": name, **tags})


# (item, expected artist, expected title, featuring, confidence)
CASES = [
    # rule 2: «Artist - Title» from the file name
    ({"name": f"Bad Bunny - Tití Me Preguntó (Official Video) {ID}.opus"}, "Bad Bunny", "Tití Me Preguntó", [], "high"),
    ({"name": f"Daddy Yankee - Gasolina (Video Oficial) {ID}.m4a"}, "Daddy Yankee", "Gasolina", [], "high"),
    ({"name": f"Shakira - Hips Don't Lie (Audio Oficial) {ID}.opus"}, "Shakira", "Hips Don't Lie", [], "high"),
    ({"name": f"Drake - Passionfruit (Lyrics) {ID}.mp3"}, "Drake", "Passionfruit", [], "high"),
    ({"name": f"Maná - Rayando el Sol (Letra) {ID}.opus"}, "Maná", "Rayando el Sol", [], "high"),
    ({"name": f"Artist - Song [Official Music Video] [HD] {ID}.opus"}, "Artist", "Song", [], "high"),
    ({"name": f"Artist - Song (Official Visualizer) {ID}.opus"}, "Artist", "Song", [], "high"),
    ({"name": f"Artist – Song (Lyric Video) {ID}.opus"}, "Artist", "Song", [], "high"),
    ({"name": f"Artist — Song (4K) {ID}.opus"}, "Artist", "Song", [], "high"),
    ({"name": f"Artist - Song - Official Video {ID}.opus"}, "Artist", "Song", [], "high"),
    ({"name": f"Taylor Swift - Clean {ID}.opus"}, "Taylor Swift", "Clean", [], "high"),
    ({"name": f'Artist - "Song" {ID}.opus'}, "Artist", "Song", [], "high"),
    ({"name": "Artist - Song.flac"}, "Artist", "Song", [], "high"),
    # versions stay in the title
    ({"name": f"Artist - Song (Slowed + Reverb) {ID}.opus"}, "Artist", "Song (Slowed + Reverb)", [], "high"),
    ({"name": f"Artist - Song (Live at Wembley) [HD] {ID}.opus"}, "Artist", "Song (Live at Wembley)", [], "high"),
    ({"name": f"Artist - Song (Acoustic Version) (Official Audio) {ID}.opus"}, "Artist", "Song (Acoustic Version)", [], "high"),
    ({"name": f"Artist - Song (Remix) {ID}.opus"}, "Artist", "Song (Remix)", [], "high"),
    # featuring
    ({"name": f"Calvin Harris ft. Rihanna - This Is What You Came For (Official Video) {ID}.opus"},
     "Calvin Harris", "This Is What You Came For", ["Rihanna"], "high"),
    ({"name": f"Artist - Song (feat. Other & Third) [Official Audio] {ID}.opus"}, "Artist", "Song", ["Other", "Third"], "high"),
    ({"name": f"Artist - Song (with Guest) {ID}.opus"}, "Artist", "Song", ["Guest"], "high"),
    ({"name": f"Artist - Song featuring Guest (Remix) {ID}.opus"}, "Artist", "Song (Remix)", ["Guest"], "high"),
    ({"name": f"Artist feat. Guest - Song {ID}.opus"}, "Artist", "Song", ["Guest"], "high"),
    # Japanese
    ({"name": f"米津玄師 - Lemon {ID}.opus"}, "米津玄師", "Lemon", [], "high"),
    ({"name": f"YOASOBI「夜に駆ける」 Official Music Video {ID}.opus"}, "YOASOBI", "夜に駆ける", [], "high"),
    ({"name": f"Aimer - 残響散歌 (Official Video) {ID}.opus"}, "Aimer", "残響散歌", [], "high"),
    # all caps
    ({"name": f"QUEEN - BOHEMIAN RHAPSODY (OFFICIAL VIDEO) {ID}.opus"}, "Queen", "Bohemian Rhapsody", [], "high"),
    ({"name": f"DJ SNAKE - LET ME LOVE YOU (LYRIC VIDEO) {ID}.opus"}, "DJ Snake", "Let Me Love You", [], "high"),
    ({"name": f"ROCKY II - EYE OF THE TIGER {ID}.opus"}, "Rocky II", "Eye Of The Tiger", [], "high"),
    ({"name": f"Rosalía - DESPECHÁ (Official Video) {ID}.opus"}, "Rosalía", "DESPECHÁ", [], "high"),  # one word: stylized
    # rule 1: real tags (YouTube Music), only cleaned
    ({"name": "x.opus", "title": "Tití Me Preguntó", "artist": "Bad Bunny - Topic"}, "Bad Bunny", "Tití Me Preguntó", [], "high"),
    ({"name": "x.flac", "title": "Bohemian Rhapsody - Remastered 2011", "artist": "Queen"}, "Queen", "Bohemian Rhapsody", [], "high"),
    ({"name": "x.flac", "title": "Wish You Were Here (2011 Remastered)", "artist": "Pink Floyd"}, "Pink Floyd", "Wish You Were Here", [], "high"),
    ({"name": "x.opus", "title": "Bad Bunny - Tití Me Preguntó (Official Video)", "artist": "BadBunnyVEVO",
      "uploader": "BadBunnyVEVO"}, "Bad Bunny", "Tití Me Preguntó", [], "high"),
    ({"name": "x.opus", "title": "Song (feat. Guest)", "artist": "Artist"}, "Artist", "Song", ["Guest"], "high"),
    # the tag is a label: right, but for a look (2026-11-07)
    ({"name": "x.opus", "title": "Real Artist - Song", "artist": "Some Records", "uploader": "Some Records"},
     "Real Artist", "Song", [], "low"),
    # rule 1 but the artist may be the channel
    ({"name": "x.opus", "title": "Me Porto Bonito", "artist": "BadBunnyVEVO"}, "Bad Bunny", "Me Porto Bonito", [], "low"),
    ({"name": "x.opus", "title": "Song (Official Video)", "artist": "Some Records", "uploader": "Some Records"},
     "Some Records", "Song", [], "low"),
    # ambiguous splits: low
    ({"name": f"Despacito - Luis Fonsi {ID}.opus", "uploader": "Luis Fonsi"}, "Luis Fonsi", "Despacito", [], "low"),
    ({"name": f"Despacito (Remix) - Luis Fonsi {ID}.opus"}, "Despacito (Remix)", "Luis Fonsi", [], "low"),
    ({"name": f"Song Title | Artist Name {ID}.opus"}, "Song Title", "Artist Name", [], "low"),
    ({"name": f"Artist - Song - Live {ID}.opus"}, "Artist", "Song - Live", [], "low"),
    ({"name": "01 - Intro.flac"}, "", "Intro", [], "low"),
    ({"name": f"Song (Official Video) {ID}.opus", "uploader": "Some Artist - Topic"}, "Some Artist", "Song", [], "low"),
]


@pytest.mark.parametrize("item,artist,title,feats,conf", CASES, ids=[c[0].get("title") or c[0]["name"] for c in CASES])
def test_propose(item, artist, title, feats, conf):
    p = propose(item)
    assert p is not None
    assert (p["artist"], p["title"], p["featuring"], p["confidence"]) == (artist, title, feats, conf)
    assert p["why"] and p["why"][0].isupper()


@pytest.mark.parametrize("item", [
    {"name": "x.opus", "title": "Tití Me Preguntó", "artist": "Bad Bunny", "album": "Un Verano Sin Ti"},
    {"name": "x.opus", "title": "Clean", "artist": "Taylor Swift"},
    {"name": "x.opus", "title": "夜に駆ける", "artist": "YOASOBI"},
    {"name": "x.opus", "title": "HUMBLE.", "artist": "Kendrick Lamar"},
    {"name": "x.opus", "title": "Song (Live)", "artist": "ABBA"},
    {"name": f"Song Without Artist {ID}.opus"},
    {"name": f"Rocky II Theme {ID}.opus"},
    {"name": "Song.flac", "title": "", "artist": ""},
    {"name": ""},
])
def test_nothing_to_change(item):
    assert propose(item) is None


def test_album_noise_and_details():
    p = propose({"name": "x.opus", "title": "Song", "artist": "Artist", "album": "Song (Official Audio)"})
    assert p["album"] == "Song" and p["title"] == "Song"
    p = propose({"name": f"Artist - Song (Official Video) {ID}.opus", "album": "Real Album"})
    assert p["album"] is None  # nothing to clean there: left as is
    # yt-dlp's Windows-safe look-alikes in file names
    p = propose({"name": f"AC⧸DC - Back In Black {ID}.opus"})
    assert (p["artist"], p["title"]) == ("AC/DC", "Back In Black")
    # an 11-letter word in brackets is not a video id
    p = propose({"name": "Artist - Song [Unreleased1].opus"})
    assert p["title"] == "Song"
    p = propose({"name": "Artist - Song [Instrumentl].opus"})
    assert p["title"] == "Song [Instrumentl]"


def test_deterministic():
    item = {"name": f"Calvin Harris ft. Rihanna - This Is What You Came For (Official Video) {ID}.opus"}
    assert all(propose(dict(item)) == propose(dict(item)) for _ in range(5))


# --- writing tags ----------------------------------------------------------

def _ffmpeg() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    # Bakin's own copy (tests point BAKIN_HOME elsewhere, so look it up directly)
    own = Path(os.environ.get("LOCALAPPDATA", "")) / "BakinData" / "tools" / "ffmpeg" / "ffmpeg.exe"
    return str(own) if own.is_file() else None


FFMPEG = _ffmpeg()
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not available")


def _make(tmp_path: Path, ext: str) -> Path:
    out = tmp_path / f"x.{ext}"
    r = subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=d=1",
                        "-metadata", "title=Old Title", "-metadata", "artist=Old Artist", str(out)],
                       capture_output=True)
    if r.returncode != 0 or not out.is_file():
        pytest.skip(f"ffmpeg cannot make .{ext}: {r.stderr.decode(errors='replace')[-200:]}")
    return out


@needs_ffmpeg
@pytest.mark.parametrize("ext", ["mp3", "opus", "flac", "m4a"])
def test_write_and_undo(tmp_path, ext):
    f = _make(tmp_path, ext)
    start = audiotags.read(f)
    snap = audiotags.snapshot(f)
    assert (snap["title"], snap["artist"], snap["album"]) == ("Old Title", "Old Artist", "")
    prev = audiotags.write(f, title="Tití Me Preguntó", artist="米津玄師", album="Un Verano")
    assert prev == snap
    now = audiotags.read(f)
    assert (now["title"], now["artist"], now["album"]) == ("Tití Me Preguntó", "米津玄師", "Un Verano")
    assert now["duration"] == start["duration"]
    # only the fields given are written
    assert audiotags.write(f, title="Other") == {"title": "Tití Me Preguntó", "artist": "米津玄師", "album": "Un Verano",
                                                 "year": ""}
    assert audiotags.snapshot(f)["artist"] == "米津玄師"
    audiotags.write(f, **prev)  # undo
    assert audiotags.read(f) == start
    assert audiotags.snapshot(f) == snap


@needs_ffmpeg
def test_write_keeps_cover(tmp_path):
    from mutagen.flac import FLAC, Picture
    f = _make(tmp_path, "flac")
    pic = Picture()
    pic.type, pic.mime, pic.data = 3, "image/jpeg", b"\xff\xd8fakejpeg"
    fl = FLAC(f)
    fl.add_picture(pic)
    fl.save()
    audiotags.write(f, title="New")
    assert audiotags.cover(f) == b"\xff\xd8fakejpeg"


@needs_ffmpeg
def test_wav_not_supported(tmp_path):
    f = _make(tmp_path, "wav")
    with pytest.raises(ValueError, match="no admite tags"):
        audiotags.write(f, title="x")


def test_not_audio(tmp_path):
    f = tmp_path / "x.opus"
    f.write_bytes(b"not audio at all")
    with pytest.raises(ValueError):
        audiotags.write(f, title="x")


@pytest.mark.parametrize("artist,title,want_artist,want_title,conf", [
    # seen on the user's library (2026-11-07): the tag is the label or the channel
    ("RHINO", "ZZ Top - Gimme All Your Lovin' (Official Music Video) [HD]", "ZZ Top", "Gimme All Your Lovin'", "low"),
    ("Song des Tages", "Madcon – Don’t Worry feat. Ray Dalton [Official Video]", "Madcon", "Don’t Worry", "low"),
    # the title's artist is one of the tagged ones, or the same with «The»
    ("bbno$, ironmouse", "bbno$ - 1-800 (ft. ironmouse)", "bbno$, ironmouse", "1-800", "high"),
    ("Black Eyed Peas", "The Black Eyed Peas - Rock That Body (Official Music Video)", "Black Eyed Peas", "Rock That Body", "high"),
    # more noise
    ("BLAKE", "BLAKE - LENGUA DE SERPIENTE [VIDEOCLIP OFICIAL]", "BLAKE", "Lengua De Serpiente", "high"),
    ("Disturbed", "Disturbed - Stupify (Official Music Video) [4K Upgrade]", "Disturbed", "Stupify", "high"),
    # the tagged artist is one of the title's
    ("Kanye West", "Jay-Z & Kanye West - No Church In The Wild ft. Frank Ocean", "Jay-Z & Kanye West", "No Church In The Wild", "high"),
    ("The Rolling Stones", "The Rolling Stones - Miss You - OFFICIAL PROMO", "The Rolling Stones", "Miss You", "high"),
])
def test_library_cases(artist, title, want_artist, want_title, conf):
    r = propose({"name": "x.opus", "artist": artist, "title": title, "album": ""})
    assert (r["artist"], r["title"], r["confidence"]) == (want_artist, want_title, conf)


def test_a_name_listed_twice_once():
    r = propose({"name": "x.opus", "artist": "TERRASPEX, Shota Horie, Shota Horie", "title": "Amazing Break", "album": ""})
    assert r["artist"] == "TERRASPEX, Shota Horie"


@needs_ffmpeg
@pytest.mark.parametrize("ext", ["mp3", "opus", "flac", "m4a"])
def test_cover_write_and_undo(tmp_path, ext):
    f = _make(tmp_path, ext)
    jpeg = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"fake jpeg" * 20
    before = audiotags.write_cover(f, jpeg)
    assert audiotags.cover(f) == jpeg
    assert audiotags.write_cover(f, before) == jpeg  # undo
    assert audiotags.cover(f) == before
    assert audiotags.snapshot(f)["title"] == "Old Title"  # tags untouched
