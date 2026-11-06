"""Duplicate songs: normalization, quality order and grouping."""

import random
import time

from bakin import dupes


def song(id, title="Song", artist="Artist", duration=200.0, codec="opus", bitrate=160,
         size=4_000_000, mtime=1000, name=None, lossless=None, **kw):
    it = {"id": id, "name": name or f"{artist} - {title}.{codec}", "title": title, "artist": artist,
          "album": "", "duration": duration, "codec": codec,
          "lossless": codec in ("flac", "alac", "pcm") if lossless is None else lossless,
          "bitrate": bitrate, "size": size, "mtime": mtime, "fav": False}
    it.update(kw)
    return it


def ids(gs):
    return [g["items"] for g in gs]


# ------------------------------------------------------------ normalize

def test_normalize_drops_upload_noise():
    n = dupes.normalize
    assert n("Song (Official Video)") == "song"
    assert n("Song [4K] (Lyrics)") == "song"
    assert n("Song (Audio)") == "song"
    assert n("Song (Remastered 2009)") == "song"
    assert n("Song - Remastered 2009") == "song"
    assert n("Song (Video Oficial)") == "song"


def test_normalize_keeps_version_words():
    n = dupes.normalize
    assert n("Song (Remix)") == "song remix"
    assert n("Song (Live)") == "song live"
    assert n("Song (Official Live Video)") == "song live"
    assert n("Song (Acoustic Version)") == "song acoustic"
    assert n("Song [Slowed + Reverb]") == "song slowed reverb"
    assert n("Song - Sped Up") == "song sped up"
    for w in ("Instrumental", "Cover", "Radio Edit", "Extended Mix", "Demo", "Karaoke", "Nightcore"):
        assert n(f"Song ({w})") != "song", w


def test_normalize_accents_case_punctuation_feat():
    n = dupes.normalize
    assert n("Canción  de   AMOR!!") == "cancion de amor"
    assert n("Song feat. Somebody") == "song"
    assert n("Song (ft. Somebody)") == "song"
    assert n("Song (feat. Somebody) [Remix]") == "song remix"
    assert n("Song (Part 2)") == "song part 2"  # unknown brackets are kept


# ------------------------------------------------------------ quality

def test_quality_tier_beats_bitrate():
    # documented rule: lossless > opus > aac/vorbis > mp3 > other, whatever the bitrate
    flac = song("f", codec="flac", bitrate=900)
    opus = song("o", codec="opus", bitrate=130)
    aac = song("a", codec="aac", bitrate=256)
    vorbis = song("v", codec="vorbis", bitrate=192)
    mp3 = song("m", codec="mp3", bitrate=320)
    other = song("x", codec="wma", bitrate=999)
    r = dupes.quality_rank
    assert r(flac) > r(opus) > r(aac) > r(mp3) > r(other)
    assert r(vorbis) > r(mp3) and r(opus) > r(vorbis)
    assert r(song("l", codec="weird", lossless=True)) > r(opus)


def test_quality_same_codec_bitrate_then_size_then_date():
    r = dupes.quality_rank
    assert r(song("a", bitrate=160)) > r(song("b", bitrate=130))
    assert r(song("a", size=5)) > r(song("b", size=4))
    assert r(song("a", mtime=2000)) > r(song("b", mtime=1000))
    assert r(song("a", bitrate=None, size=None, mtime=None)) == (3, 0.0, 0.0, 0.0)


# ------------------------------------------------------------ groups

def test_exact_dupes_in_different_formats():
    gs = dupes.groups([song("mp3", codec="mp3", bitrate=320), song("opus", codec="opus", bitrate=160),
                       song("m4a", codec="aac", bitrate=128), song("other", title="Other")])
    assert len(gs) == 1
    g = gs[0]
    assert g["items"] == ["opus", "m4a", "mp3"] and g["best"] == "opus"
    assert g["sure"] is True and g["why"] == "Mismo título, artista y duración"
    assert g["key"] == "artist|song"


def test_lossless_is_best():
    gs = dupes.groups([song("o"), song("f", codec="flac", bitrate=1000)])
    assert gs[0]["best"] == "f"


def test_remix_and_live_are_not_dupes():
    gs = dupes.groups([song("a"), song("b", title="Song (Remix)"), song("c", title="Song (Live)"),
                       song("d", title="Song (Official Video)")])
    assert ids(gs) == [["a", "d"]]


def test_feat_differences_grouped():
    gs = dupes.groups([song("a", title="Song (feat. B)", artist="Artist, B"),
                       song("b", title="Song", artist="Artist & B"),
                       song("c", title="Song ft. B", artist="Artist feat. B"),
                       song("d", title="Song", artist="Artist - Topic")])
    assert ids(gs) == [["a", "b", "c", "d"]]


def test_accents_and_case_grouped():
    gs = dupes.groups([song("a", title="Canción", artist="Beyoncé"),
                       song("b", title="CANCION", artist="beyonce")])
    assert ids(gs) == [["a", "b"]]


def test_duration_tolerance():
    assert ids(dupes.groups([song("a", duration=200), song("b", duration=201.5)])) == [["a", "b"]]
    assert dupes.groups([song("a", duration=200), song("b", duration=204)]) == []


def test_chain_is_split():
    # A~B and B~C but A and C are 5 s apart: never one group
    gs = dupes.groups([song("a", duration=200), song("b", duration=202), song("c", duration=205),
                       song("d", duration=206)])
    assert ids(gs) == [["a", "b"], ["c", "d"]]
    assert gs[0]["key"] == "artist|song" and gs[1]["key"] == "artist|song|1"
    for g in gs:
        durs = [{"a": 200, "b": 202, "c": 205, "d": 206}[i] for i in g["items"]]
        assert max(durs) - min(durs) <= 2


def test_missing_durations_need_exact_tags_and_are_not_sure():
    gs = dupes.groups([song("a", duration=200), song("b", duration=None, codec="mp3")])
    assert ids(gs) == [["a", "b"]] and gs[0]["sure"] is False
    assert "sin duración" in gs[0]["why"]
    # only the normalized title matches: not enough without a duration
    assert dupes.groups([song("a"), song("b", title="Song (Official Video)", duration=None)]) == []
    gs = dupes.groups([song("a", duration=None), song("b", duration=None, codec="mp3")])
    assert ids(gs) == [["a", "b"]] and gs[0]["sure"] is False


def test_empty_tags_fall_back_to_file_name():
    gs = dupes.groups([
        song("a", title="", artist="", name="Artist - Song [dQw4w9WgXcQ].opus"),
        song("b", title="Song", artist="Artist", codec="mp3"),
        song("c", title="", artist="", name="Artist - Song (Official Video).m4a", codec="aac"),
        song("d", title="", artist="", name="Untitled.mp3", codec="mp3"),
    ])
    assert ids(gs) == [["a", "c", "b"]]


def test_different_artists_not_grouped_and_sorted_by_artist_then_title():
    gs = dupes.groups([song("z1", artist="Zeta", title="Alpha"), song("z2", artist="Zeta", title="Alpha"),
                       song("a1", artist="Abba", title="Waterloo"), song("a2", artist="Abba", title="Waterloo"),
                       song("a3", artist="Abba", title="Mamma Mia"), song("a4", artist="Abba", title="Mamma Mia"),
                       song("x", artist="Other", title="Alpha")])
    assert ids(gs) == [["a3", "a4"], ["a1", "a2"], ["z1", "z2"]]


def test_singletons_and_bad_items_ignored():
    assert dupes.groups([]) == []
    assert dupes.groups([song("a")]) == []
    assert dupes.groups([{"id": None}, "junk", song("a", title="", name="")]) == []


def test_deterministic_regardless_of_input_order():
    items = [song(f"s{i}", title=f"T{i % 7}", artist=f"A{i % 3}", duration=200 + (i % 5) * 0.7,
                  codec=["opus", "mp3", "aac", "flac"][i % 4], bitrate=100 + i) for i in range(80)]
    first = dupes.groups(items)
    assert first
    for seed in range(5):
        shuffled = items[:]
        random.Random(seed).shuffle(shuffled)
        assert dupes.groups(shuffled) == first


def test_performance_10000_songs():
    rng = random.Random(1)
    items = []
    for i in range(10_000):
        k = i // 2 if i % 3 else i  # about a third are pairs
        items.append(song(f"id{i}", title=f"Song number {k} (Official Video)", artist=f"Artist {k % 900}, Guest",
                          duration=120 + (k % 200) + rng.random(), codec=rng.choice(["opus", "mp3", "aac"])))
    t = time.perf_counter()
    gs = dupes.groups(items)
    assert time.perf_counter() - t < 1.0
    assert gs and all(len(g["items"]) >= 2 for g in gs)
