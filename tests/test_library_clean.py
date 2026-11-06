"""PLAN 0.14 through the engine: clean titles (apply / undo), duplicates, radio, statistics."""

import shutil
import subprocess

import pytest

from bakin import mediadl, music, service, tools
from tests.test_tagclean import FFMPEG, needs_ffmpeg


@pytest.fixture
def svc(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "missing", lambda required=None: [])
    monkeypatch.setattr(service.Service, "_ensure_worker", lambda self: None)
    service.save_settings({**service.load_settings(), "out_dir": str(tmp_path / "dl")})
    return service.Service()


def music_folder(tmp_path):
    preset = next(p for p in mediadl.load_settings()["presets"] if p["id"] == "bimusic")
    out = mediadl.destination(preset, tmp_path / "dl")
    mediadl.mark_folder(out, preset)
    return out


def song(folder, name, title, artist, seconds=1):
    out = folder / name
    r = subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=d={seconds}",
                        "-metadata", f"title={title}", "-metadata", f"artist={artist}", str(out)], capture_output=True)
    if r.returncode or not out.is_file():
        pytest.skip("ffmpeg cannot make test songs")
    return out


@needs_ffmpeg
def test_clean_titles_apply_and_undo(svc, tmp_path):
    folder = music_folder(tmp_path)
    song(folder, "a.opus", "Ace of Base - All That She Wants (Official Music Video)", "Ace of Base")
    song(folder, "b.opus", "Already Clean", "Someone")
    props = svc.music_clean_propose()["items"]
    assert [(p["after"]["artist"], p["after"]["title"]) for p in props] == [("Ace of Base", "All That She Wants")]
    res = svc.music_clean_apply([{"id": props[0]["id"], **props[0]["after"]}])
    assert res["ok"] and len(res["done"]) == 1
    assert not svc.music_clean_propose()["items"]  # nothing left to clean
    assert svc.music_clean_propose()["undo"]["count"] == 1
    assert svc.music_tags_undo()["ok"]
    assert len(svc.music_clean_propose()["items"]) == 1  # as it was


@needs_ffmpeg
def test_the_song_playing_is_left_for_later(svc, tmp_path):
    folder = music_folder(tmp_path)
    song(folder, "a.opus", "X - Y (Official Video)", "X")
    p = svc.music_clean_propose()["items"][0]
    res = svc.music_clean_apply([{"id": p["id"], **p["after"]}], playing=p["id"])
    assert res["skipped"] == [p["id"]] and not res["done"]


@needs_ffmpeg
def test_duplicates_keep_one_and_carry_its_history(svc, tmp_path, monkeypatch):
    folder = music_folder(tmp_path)
    song(folder, "a.mp3", "Tema", "Artista", 2)
    song(folder, "b.opus", "Tema (Official Video)", "Artista", 2)
    groups = svc.music_dupes()["groups"]
    assert len(groups) == 1
    best, other = groups[0]["items"][0], groups[0]["items"][1]
    assert best["codec"] == "opus"
    music.record_play(other["id"])
    music.record_play(other["id"])
    pl = music.playlist_save(None, "Lista", ids=[other["id"]])
    deleted = []
    monkeypatch.setattr(service.Service, "library_delete",
                        lambda self, ids: deleted.extend(ids) or {"ok": True, "deleted": len(ids), "failed": []})
    res = svc.music_dupes_resolve(best["id"], [other["id"]])
    assert res["kept"] == best["id"] and deleted == [other["id"]]
    assert music.plays()[best["id"]]["n"] == 2
    assert music.playlist(pl["id"])["ids"] == [best["id"]]


def test_musicbrainz_needs_the_setting(svc):
    assert not svc.music_mb_start(["x"])["ok"]


@needs_ffmpeg
def test_stats_count_plays_minutes_and_artists(svc, tmp_path):
    folder = music_folder(tmp_path)
    song(folder, "a.opus", "Uno", "Artista A", 60)
    song(folder, "b.opus", "Dos", "Artista B", 30)
    ids = {i["title"]: i["id"] for i in svc._songs_by_id(None).values()}
    for _ in range(3):
        music.record_play(ids["Uno"])
    music.record_play(ids["Dos"])
    st = svc.music_stats("week")
    assert st["plays"] == 4 and st["minutes"] == round(3 + 0.5)
    assert st["top_songs"][0]["title"] == "Uno" and st["top_songs"][0]["plays"] == 3
    assert [a["artist"] for a in st["top_artists"]] == ["Artista A", "Artista B"]
    assert set(st["new_artists"]) == {"Artista A", "Artista B"}
    assert sum(st["hours"]) == 4


@needs_ffmpeg
def test_radio_view(svc, tmp_path):
    folder = music_folder(tmp_path)
    for n in range(6):
        song(folder, f"{n}.opus", f"T{n}", "Mismo" if n < 3 else f"Otro {n}")
    seed = next(i for i in svc._songs_by_id(None).values() if i["title"] == "T0")
    got = svc._query({"music": {"view": "radio", "value": seed["id"]}})
    assert got[0]["id"] == seed["id"] and len(got) == 6


def test_ffmpeg_helper_is_shared():
    assert FFMPEG is None or shutil.which(FFMPEG) or FFMPEG.endswith("ffmpeg.exe")
