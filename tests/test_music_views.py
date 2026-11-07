"""Bakin Música, stage 2: views over the songs, smart rules, mixes and
playlists (normal, smart, exported as .m3u8, kept across upgrades)."""

import time
from pathlib import Path

import pytest

from bakin import audiotags, music, service
from test_music import make, needs_ffmpeg

NOW = 1_800_000_000.0
DAY = 86400


def song(n, **kw):
    base = {"id": f"c|{n}.opus", "name": f"{n}.opus", "rel": f"{n}.opus", "path": f"C:/m/{n}.opus", "date": NOW - n * DAY,
            "title": f"Tema {n}", "artist": "", "album": "", "duration": 200, "bitrate": 130, "codec": "opus",
            "fav": False, "plays": 0, "last_played": 0}
    return {**base, **kw}


@pytest.fixture(autouse=True)
def fresh():
    music.reset_memory_cache()
    audiotags.reset_memory_cache()
    yield
    music.reset_memory_cache()


def test_rules_match_all_or_any_sort_and_limit():
    items = [song(1, artist="Daft Punk", plays=5, last_played=NOW - DAY),
             song(2, artist="Daft Punk", plays=0),
             song(3, artist="Justice", plays=9, last_played=NOW - 90 * DAY, fav=True),
             song(40, artist="Moderat", duration=600)]
    rules = {"match": "all", "rules": [{"field": "artist", "op": "contains", "value": "daft"},
                                       {"field": "plays", "op": "gte", "value": 1}]}
    assert [i["id"] for i in music.apply_rules(items, rules, NOW)] == ["c|1.opus"]
    anyr = {"match": "any", "sort": "plays", "rules": [{"field": "fav", "op": "is", "value": True},
                                                       {"field": "minutes", "op": "gte", "value": 8}]}
    assert [i["id"] for i in music.apply_rules(items, anyr, NOW)] == ["c|3.opus", "c|40.opus"]
    stale = {"rules": [{"field": "played_days", "op": "not_within", "value": 30}], "sort": "new", "limit": 2}
    assert [i["id"] for i in music.apply_rules(items, stale, NOW)] == ["c|2.opus", "c|3.opus"]
    added = {"rules": [{"field": "added_days", "op": "within", "value": 3}]}
    assert {i["id"] for i in music.apply_rules(items, added, NOW)} == {"c|1.opus", "c|2.opus", "c|3.opus"}
    junk = music.clean_rules({"rules": [{"field": "path", "op": "is", "value": "x"}, {"field": "plays", "op": "contains", "value": 1},
                                        {"field": "artist", "op": "is", "value": "  "}], "limit": "lots", "sort": "nope"})
    assert junk == {"match": "all", "rules": [], "sort": "new", "limit": 0}


def test_mixes_are_stable_within_a_day_and_need_five_songs():
    items = [song(i, plays=i % 4, last_played=(NOW - 60 * DAY) if i % 4 else 0, artist="A" if i < 10 else "B")
             for i in range(1, 41)]
    first = music.mixes(items, NOW)
    assert music.mixes(items, NOW) == first                                      # same day, same mixes
    by_id = {m["id"]: m for m in first}
    assert {"daily", "forgotten", "rediscover"} <= set(by_id)
    assert len(by_id["daily"]["ids"]) == 40 and len(set(by_id["daily"]["ids"])) == 40   # no repeats
    played_a = {i["id"] for i in items if i["artist"] == "A"}
    assert "radio:A" in by_id and played_a & set(by_id["radio:A"]["ids"])
    assert music.mixes(items[:4], NOW) == []                                     # too few songs for any mix
    tomorrow = music.mixes(items, NOW + DAY)
    assert {m["id"]: m["ids"] for m in tomorrow}["daily"] != by_id["daily"]["ids"]


def test_playlists_crud_and_upgrade_rename():
    pl = music.playlist_save(None, "  Viaje  ", "list")
    assert pl["name"] == "Viaje" and pl["ids"] == []
    assert music.playlist_add(pl["id"], ["a", "b", "a"]) == 2 and music.playlist_add(pl["id"], ["b", "c"]) == 1
    assert music.playlist(pl["id"])["ids"] == ["a", "b", "c"]
    music.rename_ids("b", "b2")
    assert music.playlist(pl["id"])["ids"] == ["a", "b2", "c"]
    assert music.playlist_remove(pl["id"], ["a", "zz"]) == 1
    smart = music.playlist_save(None, "Lo mejor", "smart", {"rules": [{"field": "plays", "op": "gte", "value": 3}]})
    assert smart["kind"] == "smart" and smart["rules"]["rules"][0]["value"] == 3.0 and "ids" not in smart
    with pytest.raises(ValueError):
        music.playlist_add(smart["id"], ["a"])                                   # smart ones fill themselves
    with pytest.raises(ValueError):
        music.playlist_save(None, "   ")
    assert music.playlist_delete(pl["id"]) and not music.playlist(pl["id"])
    assert [p["name"] for p in music.playlists()] == ["Lo mejor"]


@pytest.fixture
def lib(tmp_path):
    d = tmp_path / "Musica"
    d.mkdir()
    make(d / "Uno.opus", "opus", "Uno", "Alfa", "Primero")
    make(d / "Dos.opus", "opus", "Dos", "Alfa", "Primero")
    make(d / "Tres.opus", "opus", "Tres", "Beta", "Segundo")
    make(d / "Cuatro.mp3", "mp3", "Cuatro", "", "")
    svc = service.Service()
    svc.library_add_folder(str(d))
    ids = {t["title"]: t["id"] for t in svc.music_tracks()["tracks"]}
    return svc, ids


def titles(svc, view, **kw):
    return [i["title"] for i in svc.library_items({"music": {"view": view, **kw}}, 0, 100)["items"]]


@needs_ffmpeg
def test_views_play_counts_artists_and_albums(lib):
    svc, ids = lib
    for _ in range(3):
        svc.music_played(ids["Tres"])
    svc.music_played(ids["Uno"])
    assert titles(svc, "top") == ["Tres", "Uno"]
    assert titles(svc, "recent")[0] == "Uno"
    assert sorted(titles(svc, "never")) == ["Cuatro", "Dos"]
    assert sorted(titles(svc, "artist", value="alfa")) == ["Dos", "Uno"]
    assert titles(svc, "album", value="Segundo", artist="Beta") == ["Tres"]
    groups = svc.music_groups("artist")["groups"]
    assert [(g["name"], g["count"], g["plays"]) for g in groups] == [("Alfa", 2, 1), ("Beta", 1, 3), ("", 1, 0)]
    albums = svc.music_groups("album")["groups"]
    assert [(g["name"], g["artist"]) for g in albums][:2] == [("Primero", "Alfa"), ("Segundo", "Beta")]
    ov = svc.music_overview()
    assert (ov["songs"], ov["artists"], ov["albums"], ov["played"], ov["never"]) == (4, 2, 2, 2, 2)
    # the player plays exactly the list on screen
    assert [t["title"] for t in svc.music_tracks({"music": {"view": "top"}})["tracks"]] == ["Tres", "Uno"]


@needs_ffmpeg
def test_playlists_through_the_service_and_export(lib, tmp_path):
    svc, ids = lib
    pl = svc.music_playlist_save("Viaje")["playlist"]
    assert svc.music_playlist_add(pl["id"], [ids["Tres"], ids["Uno"], "gone|x.opus"])["added"] == 3
    assert titles(svc, "playlist", value=pl["id"]) == ["Tres", "Uno"]           # its order; missing songs skipped
    out = svc.music_playlist_export(pl["id"], str(tmp_path / "viaje"))
    assert out["ok"] and out["count"] == 2 and out["path"].endswith("viaje.m3u8")
    text = Path(out["path"]).read_text(encoding="utf-8").splitlines()
    assert text[0] == "#EXTM3U" and text[1].startswith("#EXTINF:") and "Beta - Tres" in text[1]
    assert Path(text[2]).name == "Tres.opus"
    smart = svc.music_playlist_save("Alfa", "smart", rules={"rules": [{"field": "artist", "op": "is", "value": "alfa"}],
                                                            "sort": "title"})["playlist"]
    assert titles(svc, "playlist", value=smart["id"]) == ["Dos", "Uno"]
    assert svc.library_items({"music": {"view": "rules", "rules": smart["rules"]}}, 0, 1)["total"] == 2
    lists = {p["name"]: p["count"] for p in svc.music_overview()["playlists"]}
    assert lists == {"Viaje": 2, "Alfa": 2}
    assert svc.music_playlist_export("nope", str(tmp_path / "x.m3u8"))["ok"] is False


@needs_ffmpeg
def test_overview_splits_photos_and_music(lib, tmp_path):
    svc, _ = lib
    pics = tmp_path / "Fotos"
    pics.mkdir()
    from PIL import Image
    for n in range(3):
        Image.new("RGB", (8, 8)).save(pics / f"f{n}.png")
    svc.library_add_folder(str(pics))
    ov = svc.library_overview()
    by_name = {c["name"]: c for c in ov["collections"]}
    assert by_name["Musica"]["music"] and by_name["Musica"]["audio"] == 4
    assert not by_name["Fotos"]["music"]
    assert ov["photos"] == 3 and ov["total"] == 7


@needs_ffmpeg
def test_queue_view_keeps_the_players_order(lib):
    svc, ids = lib
    order = [ids["Tres"], ids["Uno"], "gone|x.opus", ids["Dos"]]
    assert titles(svc, "queue", ids=order) == ["Tres", "Uno", "Dos"]


@needs_ffmpeg
def test_playlist_mirror_folder(lib, tmp_path, monkeypatch):
    # PLAN 0.20.1: the folder follows the playlist; removing waits for the user's first yes
    from bakin import library
    svc, ids = lib
    soon = []
    svc._mirror_soon = lambda pid, delay=None: soon.append(pid)  # no timers: the sync is run by hand
    trashed = []
    monkeypatch.setattr(library, "send_to_recycle_bin", lambda p: (trashed.append(Path(p).name), Path(p).unlink()))
    pl = svc.music_playlist_save("Auto")["playlist"]
    svc.music_playlist_add(pl["id"], [ids["Uno"], ids["Tres"]])
    car = tmp_path / "auto"
    car.mkdir()
    (car / "mio.txt").write_text("x", encoding="utf-8")
    first = svc.music_mirror_set(pl["id"], str(car))
    assert not first["ok"] and first["needs_adopt"]                       # a folder with things in it: ask
    assert svc.music_mirror_set(pl["id"], str(car), adopt=True)["ok"]
    res = svc._mirror_run(pl["id"])
    assert res["copied"] == 2 and not res["failed"]
    assert {p.name for p in car.iterdir()} >= {"Uno.opus", "Tres.opus", "playlist.m3u8", "mio.txt"}
    assert svc.music_overview()["playlists"][0]["mirror"] == str(car)

    svc.music_playlist_remove(pl["id"], [ids["Uno"]])
    assert soon.count(pl["id"]) >= 2                                      # each change schedules a sync
    res = svc._mirror_run(pl["id"])
    assert res["ask_remove"] == 1 and (car / "Uno.opus").exists() and not trashed
    assert svc.music_mirror_sync(pl["id"], allow_remove=True)["ok"]
    res = svc._mirror_run(pl["id"])
    assert res["removed"] == 1 and trashed == ["Uno.opus"] and (car / "mio.txt").exists()
    assert svc._mirror_run(pl["id"])["copied"] == 0                         # nothing left to do

    assert svc.music_mirror_off(pl["id"])["ok"] and (car / "Tres.opus").exists()
    assert svc._mirror_run(pl["id"]) is None


@needs_ffmpeg
def test_lan_library_for_the_home_server(lib):
    # PLAN 0.21.1: what the shell's OpenSubsonic server serves
    svc, ids = lib
    pl = svc.music_playlist_save("Casa")["playlist"]
    svc.music_playlist_add(pl["id"], [ids["Tres"]])
    data = svc.lan_library()
    assert sorted(s["title"] for s in data["songs"]) == ["Cuatro", "Dos", "Tres", "Uno"]
    tres = next(s for s in data["songs"] if s["id"] == ids["Tres"])
    assert tres["artist"] == "Beta" and tres["suffix"] == "opus" and tres["size"] > 0 and not tres["starred"]
    assert data["playlists"] == [{"id": pl["id"], "name": "Casa", "song_ids": [ids["Tres"]]}]
    assert svc.lan_star(ids["Tres"], True)["ok"]
    assert next(s for s in svc.lan_library()["songs"] if s["id"] == ids["Tres"])["starred"]
    svc.lan_scrobble(ids["Tres"])
    assert next(s for s in svc.lan_library()["songs"] if s["id"] == ids["Tres"])["plays"] == 1
    assert not svc.lan_star("gone|x.opus", True)["ok"] and not svc.lan_scrobble("gone|x.opus")["ok"]
    assert svc.lan_lyrics(ids["Tres"]) == {"text": ""}


def test_lan_pairing_code():
    # PLAN 0.21.1: a random code in the vault, the same until a new one is asked for
    from bakin import vault
    svc = service.Service(lambda e: None)
    first = svc.lan_code()["code"]
    assert len(first) == 9 and first[4] == "-" and not set(first.replace("-", "")) - set(svc._CODE_CHARS)
    assert svc.lan_code()["code"] == first and vault.get(svc.LAN_CODE) == first.encode()
    assert svc.lan_code(new=True)["code"] != first
    s = svc.settings_set({"lan_enabled": True, "lan_port": 80})["settings"]
    assert s["lan_enabled"] is True and s["lan_port"] == 4533


def test_mirror_open_only_its_own_folder(tmp_path, monkeypatch):
    # «Espejo ✓» › «Abrir la carpeta»: the playlist's mirror folder, or a clear «not there»
    opened = []
    monkeypatch.setattr(service.os, "startfile", lambda p: opened.append(str(p)), raising=False)
    pl = music.playlist_save(None, "Auto")
    assert not service.Service.music_mirror_open(pl["id"])["ok"]                   # no mirror
    music.playlist_mirror(pl["id"], {"folder": str(tmp_path / "gone"), "mode": "copy"})
    assert not service.Service.music_mirror_open(pl["id"])["ok"]                   # unplugged
    music.playlist_mirror(pl["id"], {"folder": str(tmp_path), "mode": "copy"})
    assert service.Service.music_mirror_open(pl["id"])["ok"] and opened == [str(tmp_path)]
