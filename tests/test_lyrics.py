"""bakin.lyrics: LRC parsing, LRCLIB lookups (fake session, no network) and
the local .lrc files."""

from pathlib import Path

import pytest
import requests

from bakin import __version__, lyrics
from bakin.paths import app_dir


# ------------------------------------------------------------------ fakes

class FakeResp:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code = status
        self._data = data
        self.headers = headers or {}
        self.closed = False

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data

    def close(self):
        self.closed = True


class FakeSession:
    """Answers by endpoint ("get" / "search"); records every call."""

    def __init__(self, **answers):
        self.answers = answers
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {}),
                           "timeout": timeout})
        ans = self.answers[url.rsplit("/", 1)[-1]]
        if isinstance(ans, Exception):
            raise ans
        return ans


HIT = {"id": 1, "trackName": "Song", "artistName": "Artist", "albumName": "Album", "duration": 200,
       "instrumental": False, "plainLyrics": "hola\nchau", "syncedLyrics": "[00:01.00]hola\n[00:02.00]chau"}


# -------------------------------------------------------------- parse_lrc

def test_parse_basic_and_metadata_ignored():
    r = lyrics.parse_lrc("[ar:Artist]\n[ti:Title]\n[length:03:20]\n[00:01.50]one\n[01:23.45]two\n")
    assert r["synced"] == [[1.5, "one"], [83.45, "two"]]
    assert r["plain"] == "one\ntwo"


def test_parse_colon_fraction_and_short_minutes():
    r = lyrics.parse_lrc("[01:23:45]colon\n[1:02.5]short\n[00:03]bare\n[00:04.123]ms")
    assert r["synced"] == [[3.0, "bare"], [4.123, "ms"], [62.5, "short"], [83.45, "colon"]]


def test_parse_multiple_stamps_and_sorting():
    r = lyrics.parse_lrc("[00:20.00]verse\n[00:10.00][00:30.00]chorus\n")
    assert r["synced"] == [[10.0, "chorus"], [20.0, "verse"], [30.0, "chorus"]]
    assert r["plain"] == "verse\nchorus"


def test_parse_positive_offset_shows_lyrics_earlier():
    r = lyrics.parse_lrc("[offset:+250]\n[00:01.00]a\n[00:00.10]b")
    assert r["synced"] == [[0.0, "b"], [0.75, "a"]]


def test_parse_negative_offset():
    r = lyrics.parse_lrc("[offset:-500]\n[00:01.00]a")
    assert r["synced"] == [[1.5, "a"]]


def test_parse_empty_lines_kept_crlf_and_bom():
    r = lyrics.parse_lrc("﻿[00:01.00]a\r\n[00:05.00]\r\n[00:09.00]b\r\n")
    assert r["synced"] == [[1.0, "a"], [5.0, ""], [9.0, "b"]]
    assert r["plain"] == "a\n\nb"


def test_parse_plain_only_text():
    r = lyrics.parse_lrc("﻿primera\r\nsegunda\n")
    assert r == {"synced": [], "plain": "primera\nsegunda"}


def test_parse_empty():
    assert lyrics.parse_lrc("") == {"synced": [], "plain": ""}


# ----------------------------------------------------------------- lookup

def test_lookup_hit_sends_only_the_four_fields_and_the_ua():
    s = FakeSession(get=FakeResp(200, HIT))
    r = lyrics.lookup("Song", "Artist", "Album", 200.4, session=s)
    assert r == {"lrc": HIT["syncedLyrics"], "plain": "hola\nchau", "instrumental": False, "source": "lrclib"}
    assert len(s.calls) == 1
    call = s.calls[0]
    assert call["url"] == "https://lrclib.net/api/get"
    assert call["params"] == {"track_name": "Song", "artist_name": "Artist", "album_name": "Album",
                              "duration": 200}
    assert call["headers"]["User-Agent"] == f"Bakin/{__version__} (https://github.com/BladeHoly/bakin)"
    assert call["timeout"]


def test_lookup_404_falls_back_to_search_within_2s():
    other = dict(HIT, duration=150, syncedLyrics="[00:01.00]wrong")
    near = dict(HIT, duration=201.5, syncedLyrics="[00:01.00]right")
    s = FakeSession(get=FakeResp(404), search=FakeResp(200, [other, near]))
    r = lyrics.lookup("Song", "Artist", "Other album", 200, session=s)
    assert r["lrc"] == "[00:01.00]right"
    assert [c["url"].rsplit("/", 1)[-1] for c in s.calls] == ["get", "search"]
    assert s.calls[1]["params"] == {"track_name": "Song", "artist_name": "Artist"}


def test_lookup_search_outside_2s_is_none():
    s = FakeSession(get=FakeResp(404), search=FakeResp(200, [dict(HIT, duration=203)]))
    assert lyrics.lookup("Song", "Artist", "Album", 200, session=s) is None
    assert len(s.calls) == 2


def test_lookup_both_404_is_none():
    s = FakeSession(get=FakeResp(404), search=FakeResp(404))
    assert lyrics.lookup("Song", "Artist", "Album", 200, session=s) is None


def test_lookup_without_album_goes_straight_to_search():
    s = FakeSession(search=FakeResp(200, [dict(HIT, duration=199)]))
    r = lyrics.lookup("Song", "Artist", "", 200, session=s)
    assert r["plain"] == "hola\nchau"
    assert len(s.calls) == 1 and s.calls[0]["url"].endswith("/search")


def test_lookup_instrumental():
    inst = dict(HIT, instrumental=True, plainLyrics=None, syncedLyrics=None)
    s = FakeSession(get=FakeResp(200, inst))
    assert lyrics.lookup("Song", "Artist", "Album", 200, session=s) == {
        "lrc": None, "plain": "", "instrumental": True, "source": "lrclib"}


def test_lookup_429_raises_busy_with_retry_after():
    resp = FakeResp(429, headers={"Retry-After": "120"})
    s = FakeSession(get=resp)
    with pytest.raises(lyrics.Busy) as e:
        lyrics.lookup("Song", "Artist", "Album", 200, session=s)
    assert e.value.retry_after == 120
    assert resp.closed and len(s.calls) == 1


def test_lookup_429_without_header_defaults_to_60():
    s = FakeSession(get=FakeResp(404), search=FakeResp(429))
    with pytest.raises(lyrics.Busy) as e:
        lyrics.lookup("Song", "Artist", "Album", 200, session=s)
    assert e.value.retry_after == 60
    assert isinstance(e.value, lyrics.LyricsError)


def test_lookup_network_error():
    s = FakeSession(get=requests.ConnectionError("boom"))
    with pytest.raises(lyrics.LyricsError, match="LRCLIB"):
        lyrics.lookup("Song", "Artist", "Album", 200, session=s)
    assert len(s.calls) == 1


def test_lookup_server_error_and_bad_json():
    with pytest.raises(lyrics.LyricsError, match="500"):
        lyrics.lookup("S", "A", "B", 200, session=FakeSession(get=FakeResp(500)))
    with pytest.raises(lyrics.LyricsError):
        lyrics.lookup("S", "A", "B", 200, session=FakeSession(get=FakeResp(200, ValueError("x"))))


def test_lookup_without_title_or_artist_sends_nothing():
    s = FakeSession()
    assert lyrics.lookup("", "Artist", "Album", 200, session=s) is None
    assert lyrics.lookup("Song", "  ", "Album", 200, session=s) is None
    assert s.calls == []


# ------------------------------------------------------------ local files

def test_local_path_both_modes(tmp_path):
    song = tmp_path / "Music" / "Artist - Song.mp3"
    assert lyrics.local_path(song, True) == tmp_path / "Music" / "Artist - Song.lrc"
    p = lyrics.local_path(song, False)
    assert p.parent == app_dir() / "music" / "lyrics"
    assert p.suffix == ".lrc" and len(p.stem) == 40
    assert lyrics.local_path(song, False) == p                       # stable
    assert lyrics.local_path(tmp_path / "other.mp3", False) != p


def test_find_in_either_place(tmp_path):
    a, b = tmp_path / "a.mp3", tmp_path / "b.mp3"
    assert lyrics.find(a) is None
    lyrics.local_path(a, True).write_text("[00:01.00]beside", encoding="utf-8")
    lyrics.save(b, "[00:01.00]appdir", beside=False)
    assert lyrics.find(a) == "[00:01.00]beside"
    assert lyrics.find(b) == "[00:01.00]appdir"


def test_find_reads_bom(tmp_path):
    song = tmp_path / "s.flac"
    lyrics.local_path(song, True).write_bytes("﻿[00:01.00]á".encode("utf-8"))
    assert lyrics.find(song) == "[00:01.00]á"


@pytest.mark.parametrize("beside", [True, False])
def test_save_round_trip(tmp_path, beside):
    song = tmp_path / "Canción.m4a"
    text = "[00:01.00]ñandú\n[00:02.00]\n[00:03.00]fin\n"
    p = lyrics.save(song, text, beside)
    assert p == lyrics.local_path(song, beside)
    raw = p.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert raw.decode("utf-8") == text
    assert lyrics.find(song) == text
    assert [f.name for f in p.parent.iterdir() if f.name.endswith(".tmp")] == []
    # overwrite in place
    lyrics.save(song, "solo texto", beside)
    assert lyrics.find(song) == "solo texto"
    assert lyrics.parse_lrc(lyrics.find(song)) == {"synced": [], "plain": "solo texto"}
