"""Exportar / importar (PLAN 0.18.1)."""

import json
import shutil
import zipfile
from pathlib import Path

import pytest

from bakin import backup, library, loudness, mediadl, music, relocate
from bakin.paths import app_dir


@pytest.fixture(autouse=True)
def _fresh_plays():
    music.reset_memory_cache()  # plays are cached in memory per process
    yield
    music.reset_memory_cache()


@pytest.fixture
def world(tmp_path):
    """A small, realistic Bakin: a music folder (Bakin's) and a photo folder (the user's)."""
    root = tmp_path / "Descargas"
    musica = root / "Música"
    musica.mkdir(parents=True)
    (musica / relocate.MEDIA_MARKER).write_text('{"preset": "Música"}', encoding="utf-8")
    for n in ("a.mp3", "b.mp3", "c.mp3"):
        (musica / n).write_bytes(b"x")
    fotos = tmp_path / "Fotos"
    fotos.mkdir()
    (fotos / "p.jpg").write_bytes(b"x")
    library.save_user_folders([fotos])
    cols = {c.id: c for c in library.collections(root)}
    mcol, fcol = cols["md:Música"], cols[library.folder_id(fotos)]

    meta = library.Meta(mcol)
    meta.update("a.mp3", fav=True, tags=["rock"])
    meta.update("b.mp3", tags=["chill"])
    meta.save()
    meta = library.Meta(fcol)
    meta.update("p.jpg", fav=True)
    meta.save()

    a, b = f"{mcol.id}|a.mp3", f"{mcol.id}|b.mp3"
    music.record_play(a)
    music.record_play(a)
    music.record_play(b)
    pl = music.playlist_save(None, "Favoritas", ids=[a, b])
    music.playlist_save(None, "Rock", kind="smart", rules={"rules": [{"field": "title", "op": "contains", "value": "x"}]})
    (app_dir() / "music" / "skips.json").write_text(json.dumps({b: {"n": 2, "last": 100.0}}), encoding="utf-8")
    (app_dir() / "music" / "loudness.json").write_text(json.dumps({a: {"lufs": -14.0, "peak_db": -1.0, "size": 1, "mtime": 1}}),
                                                       encoding="utf-8")
    lyr = app_dir() / "music" / "lyrics"
    lyr.mkdir(parents=True)
    (lyr / ("0" * 40 + ".lrc")).write_text("[00:01.00]hola", encoding="utf-8")

    ms = mediadl.load_settings()
    mine = mediadl.new_preset("Con cookies", id="mycookies", cookies_mode="always", cookies_source="file",
                              cookies_file=r"C:\secreto\cookies.txt")
    ms["presets"].append(mine)
    ms["cookies_imported_from"] = r"C:\secreto\cookies.txt"
    mediadl.save_settings(ms)

    (app_dir() / "ui-settings.json").write_text(json.dumps({"out_dir": str(root), "musicbrainz": True,
                                                            "api_token": "NO"}), encoding="utf-8")
    (app_dir() / "follows.json").write_text(json.dumps({"follows": [
        {"id": "f1", "url": "https://www.youtube.com/@canal", "kind": "media", "preset": "mycookies"}]}),
        encoding="utf-8")
    (app_dir() / "queue.json").write_text(json.dumps({"items": [], "holds": {}}), encoding="utf-8")

    # what must never travel
    for d, f in (("vault", "yt.bin"), ("tools", "yt-dlp.exe"), ("logs", "bakin.log"),
                 ("thumbs", "ab/x.jpg"), ("cache", "c.json")):
        p = app_dir() / d / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"SECRET")
    (app_dir() / "cookies.txt").write_text("SECRET", encoding="utf-8")
    (app_dir() / "cookies-health.json").write_text("{}", encoding="utf-8")
    return {"root": root, "mcol": mcol, "fcol": fcol, "a": a, "b": b, "pl": pl, "fotos": fotos, "tmp": tmp_path}


def _wipe():
    music.reset_memory_cache()
    shutil.rmtree(app_dir())


def _favs(col):
    return {rel: rec for rel, rec in library.Meta(col).items.items()}


def test_export_writes_dated_zip_with_manifest(world):
    out = world["tmp"] / "out"
    out.mkdir()
    res = backup.export(out, download_root=world["root"])
    p = Path(res["path"])
    assert p.parent == out and p.name.startswith("Bakin-respaldo-") and p.suffix == ".zip"
    assert res["size"] == p.stat().st_size and res["files"] > 5
    with zipfile.ZipFile(p) as z:
        names = set(z.namelist())
        man = json.loads(z.read("manifest.json"))
    assert man["schema"] == 1 and man["bakin_version"] and man["created"]
    assert set(man["files"]) == names - {"manifest.json"}
    assert man["collections"] == {world["mcol"].id: str(world["mcol"].root), world["fcol"].id: str(world["fcol"].root)}
    assert {"music/plays.json", "music/plays-log.jsonl", "music/skips.json", "music/playlists.json",
            "music/loudness.json", "media.json", "ui-settings.json", "follows.json", "queue.json",
            "library-folders.json", "music/lyrics/" + "0" * 40 + ".lrc"} <= names
    assert {n for n in names if n.startswith("meta/")} == set(man["meta"].values())
    # a second one the same day doesn't overwrite the first
    assert backup.export(out, download_root=world["root"])["path"] != str(p)


def test_export_never_carries_secrets_or_private_folders(world):
    res = backup.export(world["tmp"] / "x.zip", download_root=world["root"])
    assert res["path"] == str(world["tmp"] / "x.zip")
    with zipfile.ZipFile(res["path"]) as z:
        names = z.namelist()
        blob = b"".join(z.read(n) for n in names)
        media = json.loads(z.read("media.json"))
        ui = json.loads(z.read("ui-settings.json"))
    for bad in ("vault", "tools", "logs", "thumbs", "cache", "cookies", "backups", "player.json"):
        assert not any(bad in n for n in names), bad
    assert b"SECRET" not in blob and b"secreto" not in blob
    assert all(p["cookies_file"] == "" for p in media["presets"])
    assert media["cookies_imported_from"] == ""
    assert "api_token" not in ui and ui["musicbrainz"] is True
    assert all(backup.allowed(n) for n in names)


def test_round_trip(world):
    before = {"plays": dict(music.plays()), "playlists": music.playlists(), "log": music.play_log(),
              "m": _favs(world["mcol"]), "f": _favs(world["fcol"]), "loud": loudness.load(),
              "presets": {p["id"] for p in mediadl.load_settings()["presets"]}}
    z = backup.export(world["tmp"] / "b.zip", download_root=world["root"])["path"]
    _wipe()
    (world["mcol"].root / library.META_FILE).unlink()  # Bakin's own folder keeps its meta inside
    res = backup.import_(Path(z), download_root=world["root"])
    assert res["folders"] == 1 and res["collections_skipped"] == []
    assert res["plays"] == 3 and res["playlists_added"] == 2 and res["lyrics"] == 1
    assert {k: v["n"] for k, v in music.plays().items()} == {k: v["n"] for k, v in before["plays"].items()}
    assert music.playlists() == before["playlists"]
    assert sorted(music.play_log()) == sorted(before["log"])
    assert _favs(world["mcol"]) == before["m"] and _favs(world["fcol"]) == before["f"]
    assert loudness.load() == before["loud"]
    assert music.skips()[world["b"]]["n"] == 2
    assert before["presets"] <= {p["id"] for p in mediadl.load_settings()["presets"]}
    mine = next(p for p in mediadl.load_settings()["presets"] if p["id"] == "mycookies")
    assert mine["cookies_file"] == "" and mine["cookies_source"] == "browser" and mine["cookies_mode"] == "off"
    assert json.loads((app_dir() / "follows.json").read_text(encoding="utf-8"))["follows"][0]["url"].endswith("@canal")
    assert (app_dir() / "music" / "lyrics" / ("0" * 40 + ".lrc")).read_text(encoding="utf-8") == "[00:01.00]hola"
    ui = json.loads((app_dir() / "ui-settings.json").read_text(encoding="utf-8"))
    assert ui["out_dir"] == str(world["root"]) and ui["musicbrainz"] is True and "api_token" not in ui
    assert not (app_dir() / "vault").exists() and not (app_dir() / "queue.json").exists()


def test_merge_never_deletes_and_sums(world):
    a, b = world["a"], world["b"]
    z = backup.export(world["tmp"] / "b.zip", download_root=world["root"])["path"]
    # meanwhile, here: other favourites/tags, a newer playlist, a new one, more plays
    meta = library.Meta(world["mcol"])
    meta.update("a.mp3", fav=False, tags=["pop"])
    meta.update("c.mp3", fav=True)
    meta.save()
    music.record_play(b)
    music.playlist_save(world["pl"]["id"], "Favoritas (aquí)", ids=[b])
    here_only = music.playlist_save(None, "Solo aquí", ids=[a])
    # and the backup's copy of «Favoritas» is older than this one: kept here
    res = backup.import_(Path(z), download_root=world["root"])
    items = library.Meta(world["mcol"]).items
    assert items["a.mp3"]["fav"] is True and items["a.mp3"]["tags"] == ["pop", "rock"]
    assert items["c.mp3"]["fav"] is True and items["b.mp3"]["tags"] == ["chill"]
    p = music.plays()
    assert p[a]["n"] == 4 and p[b]["n"] == 3  # 2 + 2, (1 + 1) + 1
    assert res["plays"] == 3 and res["favourites"] == 1 and res["tags"] == 1
    lists = {x["id"]: x for x in music.playlists()}
    assert lists[world["pl"]["id"]]["name"] == "Favoritas (aquí)" and here_only["id"] in lists
    assert res["playlists_added"] == 0 and res["playlists_updated"] == 0
    assert len(music.play_log()) == 4  # the log isn't duplicated
    assert music.skips()[b]["n"] == 4


def test_newer_playlist_from_backup_wins_and_plays_not_counted_twice(world):
    pid = world["pl"]["id"]
    data = json.loads((app_dir() / "music" / "playlists.json").read_text(encoding="utf-8"))
    data["playlists"][0]["updated"] = data["playlists"][0]["created"] + 1000
    data["playlists"][0]["name"] = "Nueva"
    (app_dir() / "music" / "playlists.json").write_text(json.dumps(data), encoding="utf-8")
    z = backup.export(world["tmp"] / "b.zip", download_root=world["root"])["path"]
    data["playlists"][0].update(name="Vieja", updated=0)
    (app_dir() / "music" / "playlists.json").write_text(json.dumps(data), encoding="utf-8")
    res = backup.import_(Path(z), download_root=world["root"])
    assert res["playlists_updated"] == 1 and music.playlist(pid)["name"] == "Nueva"
    again = backup.import_(Path(z), download_root=world["root"])
    assert again["already_imported"] is True and again["plays"] == 0
    assert music.plays()[world["a"]]["n"] == 4  # 2 + 2 once, not 2 + 2 + 2


def test_safety_copy_made_first(world):
    z = backup.export(world["tmp"] / "b.zip", download_root=world["root"])["path"]
    res = backup.import_(Path(z), download_root=world["root"])
    safety = Path(res["safety"])
    assert safety.parent == app_dir() / "backups" and safety.name.startswith("before-import-")
    with zipfile.ZipFile(safety) as s:
        assert json.loads(s.read("music/plays.json"))[world["a"]]["n"] == 2  # the state before
    # backups/ itself is never exported
    with zipfile.ZipFile(backup.export(world["tmp"] / "c.zip", download_root=world["root"])["path"]) as z2:
        assert not any("backups" in n or "before-import" in n for n in z2.namelist())


def test_inspect_counts(world):
    z = backup.export(world["tmp"] / "b.zip", download_root=world["root"])["path"]
    before = (app_dir() / "music" / "plays.json").read_bytes()
    info = backup.inspect(Path(z))
    c = info["counts"]
    assert c["playlists"] == 2 and c["plays"] == 3 and c["songs_played"] == 2
    assert c["favourites"] == 2 and c["tags"] == 2 and c["tagged"] == 2
    assert c["follows"] == 1 and c["lyrics"] == 1 and c["collections"] == 2 and c["folders"] == 1
    assert c["presets"] == len(mediadl.load_settings()["presets"])
    assert info["manifest"]["schema"] == 1 and info["manifest"]["collections"]
    assert "3 reproducciones de 2 temas" in info["lines"] and "1 letra" in info["lines"]
    assert (app_dir() / "music" / "plays.json").read_bytes() == before


def _zip(path, entries):
    with zipfile.ZipFile(path, "w") as z:
        for n, data in entries.items():
            z.writestr(n, data)
    return path


GOOD = json.dumps({"schema": 1, "bakin_version": "0.18.1", "files": [], "collections": {}})


@pytest.mark.parametrize("entries", [
    {"music/plays.json": "{}"},                                        # no manifest
    {"manifest.json": json.dumps({"schema": 99})},                     # another schema
    {"manifest.json": "not json"},
])
def test_foreign_zip_rejected(tmp_path, entries):
    with pytest.raises(backup.BackupError, match="no es una copia de Bakin"):
        backup.inspect(_zip(tmp_path / "x.zip", entries))


def test_not_a_zip_rejected(tmp_path):
    (tmp_path / "x.zip").write_text("hola", encoding="utf-8")
    with pytest.raises(backup.BackupError, match="no es una copia de Bakin"):
        backup.import_(tmp_path / "x.zip")


@pytest.mark.parametrize("bad", ["../evil.json", "/abs.json", "C:/x.json", "music/../../x.json",
                                 "music\\..\\x.json", "vault/yt.bin", "tools/yt-dlp.exe", "logs/bakin.log",
                                 "meta/../../x.json", "music/lyrics/../x.lrc", "player.json", "cookies.txt"])
def test_zip_slip_and_unknown_entries_rejected(tmp_path, bad):
    z = _zip(tmp_path / "x.zip", {"manifest.json": GOOD, bad: "{}"})
    with pytest.raises(backup.BackupError, match="no reconoce"):
        backup.inspect(z)
    with pytest.raises(backup.BackupError):
        backup.import_(z)
    assert not (app_dir() / "backups").exists()  # rejected before the safety copy or any change
    assert not (tmp_path / "evil.json").exists() and not (tmp_path / "x.json").exists()


def test_unmatched_collection_is_skipped_not_created(world, tmp_path):
    other = json.dumps({"schema": 1, "files": ["meta/dir_abc.json"], "collections": {"dir:abc": r"Z:\\nope"},
                        "meta": {"dir:abc": "meta/dir_abc.json"}})
    z = _zip(tmp_path / "o.zip", {"manifest.json": other,
                                  "meta/dir_abc.json": json.dumps({"items": {"x.jpg": {"fav": True}}})})
    res = backup.import_(z, download_root=world["root"])
    assert res["collections_skipped"] == [r"Z:\\nope"] and res["favourites"] == 0


def test_only_merge_mode(world):
    z = backup.export(world["tmp"] / "b.zip", download_root=world["root"])["path"]
    with pytest.raises(backup.BackupError):
        backup.import_(Path(z), mode="replace", download_root=world["root"])
