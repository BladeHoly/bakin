"""Bakin Música's player support in the engine: the songs of a list, play
counts that follow a song to its better copy, and the saved player state."""

import json
import os
from pathlib import Path

import pytest

from bakin import audiotags, music, service
from test_music import make, needs_ffmpeg


@pytest.fixture(autouse=True)
def fresh():
    music.reset_memory_cache()
    audiotags.reset_memory_cache()
    yield
    music.reset_memory_cache()


@pytest.fixture
def songs(tmp_path):
    d = tmp_path / "Musica"
    d.mkdir()
    make(d / "Uno [aaaaaaaaaaa].opus", "opus", "Uno", "Alfa", "Disco")
    make(d / "Dos [bbbbbbbbbbb].opus", "opus", "Dos", "Beta")
    make(d / "Tres.mp3", "mp3", "", "")
    from PIL import Image
    Image.new("RGB", (8, 8)).save(d / "tapa.png")
    return d


@needs_ffmpeg
def test_tracks_of_a_list_and_by_id(songs):
    svc = service.Service()
    svc.library_add_folder(str(songs))
    tracks = svc.music_tracks({"sort": "title"})["tracks"]
    assert [t["title"] for t in tracks] == ["Dos", "Uno", "Tres"]   # audio only; untitled last, named after its file
    assert tracks[1]["artist"] == "Alfa" and tracks[1]["album"] == "Disco" and tracks[1]["duration"] > 1
    assert all(Path(t["path"]).is_file() for t in tracks)
    ids = [tracks[1]["id"], "nope|x.opus", tracks[0]["id"]]
    assert [t["title"] for t in svc.music_tracks(ids=ids)["tracks"]] == ["Uno", "Dos"]   # given order, unknown ids dropped
    assert svc.music_cover("nope")["cover"] == ""


@needs_ffmpeg
def test_plays_are_counted_and_follow_an_upgrade(songs):
    svc = service.Service()
    svc.library_add_folder(str(songs))
    old = next(t for t in svc.music_tracks()["tracks"] if t["name"] == "Tres.mp3")
    assert svc.music_played(old["id"])["plays"] == 1
    assert svc.music_played(old["id"])["plays"] == 2
    new = songs / "Tres.opus"
    make(new, "opus", "Tres", "")

    class Stats:
        files = [str(new)]
    svc._finish_upgrade({"id": old["id"], "path": old["path"], "yt_id": "ccccccccccc"}, Stats())
    music.reset_memory_cache()
    plays = json.loads((Path(os.environ["BAKIN_HOME"]) / "music" / "plays.json").read_text(encoding="utf-8"))
    col = old["id"].split("|")[0]
    assert old["id"] not in plays and plays[f"{col}|Tres.opus"]["n"] == 2


def test_player_state_is_cleaned_and_kept():
    s = music.save_state({"ids": ["a|1", "a|2", "a|3", 7], "order": [2, 0, 1], "pos": 9, "time": -4,
                          "volume": 3, "repeat": "loop", "speed": 9, "crossfade": 40, "shuffle": 1})
    assert s["ids"] == ["a|1", "a|2", "a|3"] and s["order"] == [2, 0, 1]
    assert (s["pos"], s["time"], s["volume"], s["repeat"], s["speed"], s["crossfade"], s["shuffle"]) == (2, 0, 1, "off", 2, 12, True)
    assert music.load_state() == s
    assert music.clean_state({"ids": ["x", "y"], "order": [0, 0]})["order"] == [0, 1]   # a broken order is rebuilt
    assert music.load_state() == s
