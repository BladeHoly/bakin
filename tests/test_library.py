"""The library (bakin.library + bakin.service): reads what is on disk."""

import os
import struct
from pathlib import Path

import pytest

from bakin import library, service
from conftest import jpeg_bytes


@pytest.fixture()
def dl(tmp_path):
    """A folder Bakin downloaded into (marked like Wallhaven's): four images,
    newest first NEW_01, NEW_02, MID_01, OLD_01, in the download folder the
    service is configured to use."""
    root = tmp_path / "dl"
    wh = root / "Wallhaven"
    (wh / "Populares").mkdir(parents=True)
    (wh / ".bakin-media.json").write_text('{"preset": "Wallhaven"}', encoding="utf-8")
    base = 1767225600
    for n, (name, size) in enumerate([("OLD_01", (1084, 1355)), ("MID_01", (1080, 1350)),
                                      ("NEW_02", (1444, 1805)), ("NEW_01", (1440, 1800))]):
        f = wh / "Populares" / f"{name}.jpg"
        f.write_bytes(jpeg_bytes(*size))
        os.utime(f, (base + n * 1000, base + n * 1000))
    (wh / ".wallhaven-archive.txt").write_text("x\n", encoding="utf-8")
    service.save_settings({**service.load_settings(), "out_dir": str(root)})
    return root


@pytest.fixture()
def mine(tmp_path):
    """One of the user's own folders: an image, a video, a song, a document."""
    d = tmp_path / "Mis cosas"
    (d / "viaje").mkdir(parents=True)
    (d / "viaje" / "playa.png").write_bytes(jpeg_bytes(300, 200))
    (d / "clip.mp4").write_bytes(b"\0" * 2000)
    (d / "tema.mp3").write_bytes(b"\0" * 3000)
    (d / "notas.pdf").write_bytes(b"%PDF")
    (d / ".hidden").write_bytes(b"x")
    return d


# ------------------------------------------------------- the general library

def test_items_newest_first_and_no_internal_files(dl):
    svc = service.Service()
    res = svc.library_items({}, 0, 3)
    assert res["total"] == 4
    assert [Path(i["rel"]).stem for i in res["items"]] == ["NEW_01", "NEW_02", "MID_01"]
    first = res["items"][0]
    assert first["kind"] == "image"
    # the preview is a file of Bakin's preview cache (PLAN 0.17.1), loaded through asset:
    thumb = Path(first["thumb"])
    assert thumb.is_file() and thumb.suffix == ".jpg" and "thumbs" in thumb.parts and first["col"] == "md:Wallhaven"
    everything = svc.library_items({}, 0, 100)["items"]
    assert not any(i["name"].endswith((".json", ".txt")) for i in everything)  # marks, archive


def test_own_folder_any_file_type(dl, mine):
    svc = service.Service()
    assert svc.library_add_folder(str(mine))["ok"]
    ov = svc.library_overview()
    assert ov["total"] == 8 and ov["kinds"] == {"image": 5, "video": 1, "audio": 1, "other": 1}
    names = {c["name"] for c in ov["collections"]}
    assert names == {"Wallhaven", "Mis cosas"}
    vids = svc.library_items({"kinds": ["video", "audio"]})["items"]
    assert sorted(i["name"] for i in vids) == ["clip.mp4", "tema.mp3"] and not vids[0]["thumb"]
    assert str(mine.resolve()) in svc.library_roots()
    # Bakin never writes into the user's folder.
    assert sorted(p.name for p in mine.rglob("*")) == [".hidden", "clip.mp4", "notas.pdf", "playa.png", "tema.mp3", "viaje"]


def test_overlapping_or_missing_folders_refused(dl, mine, tmp_path):
    svc = service.Service()
    assert not svc.library_add_folder(str(dl))["ok"]
    assert not svc.library_add_folder(str(dl / "Wallhaven"))["ok"]
    assert not svc.library_add_folder(str(tmp_path))["ok"]           # contains the downloads
    assert not svc.library_add_folder(str(tmp_path / "nope"))["ok"]
    assert not svc.library_add_folder("relativa")["ok"]
    assert svc.library_add_folder(str(mine))["ok"]
    assert not svc.library_add_folder(str(mine / "viaje"))["ok"]     # inside an added one


def test_favorites_tags_and_filters(dl, mine):
    svc = service.Service()
    svc.library_add_folder(str(mine))
    items = {i["name"]: i["id"] for i in svc.library_items({}, 0, 100)["items"]}
    new1 = items["NEW_01.jpg"]
    svc.library_set_favorite([new1, items["playa.png"]], True)
    svc.library_tag([new1, items["clip.mp4"]], add=["Cosplay ", "rosa"])
    svc.library_tag([items["clip.mp4"]], add=["cosplay"])            # same tag, any case: once
    svc.library_tag([new1], remove=["ROSA"])

    favs = svc.library_items({"favorites": True})["items"]
    assert sorted(i["name"] for i in favs) == ["NEW_01.jpg", "playa.png"]
    tagged = svc.library_items({"tags": ["cosplay"]})["items"]
    assert sorted(i["name"] for i in tagged) == ["NEW_01.jpg", "clip.mp4"]
    assert next(i for i in tagged if i["name"] == "clip.mp4")["tags"] == ["Cosplay", "rosa"]
    assert svc.library_items({"q": "rosa"})["total"] == 1
    assert svc.library_items({"collections": ["md:Wallhaven"], "favorites": True})["total"] == 1
    assert {t["name"]: t["count"] for t in svc.library_overview()["tags"]} == {"Cosplay": 2, "rosa": 1}
    # Survives a restart (written to disk); a Bakin folder's tags travel inside it.
    fresh = service.Service().library_items({"favorites": True})
    assert fresh["total"] == 2
    assert (dl / "Wallhaven" / library.META_FILE).is_file()


def test_ids_cannot_escape_their_collection(dl, monkeypatch):
    opened = []
    monkeypatch.setattr(service.os, "startfile", lambda p: opened.append(p), raising=False)
    svc = service.Service()
    good = svc.library_items({})["items"][0]["id"]
    (dl / "outside.txt").write_text("x")
    for bad in ["md:Wallhaven|../outside.txt", "md:Wallhaven|C:/Windows/win.ini", "md:nope|x.jpg",
                "md:Wallhaven|", "dir:zzz|a.jpg", "md:Wallhaven|/etc/passwd"]:
        assert svc.library_open(bad) is False
    assert svc.library_open(good) is True and len(opened) == 1
    assert svc.library_set_favorite(["md:Wallhaven|../outside.txt"], True)["changed"] == 0


# ---------------------------------------------------------------- deletion

@pytest.fixture()
def fake_bin(monkeypatch, tmp_path):
    """A Recycle Bin in a temp folder, in Windows' $I/$R format, instead of
    the real one (tests must never touch the user's Recycle Bin)."""
    bin_root = tmp_path / "$Recycle.Bin"
    sid = bin_root / "S-1-5-21-test"
    sid.mkdir(parents=True)
    n = [0]

    def send(path):
        n[0] += 1
        name = f"ABC{n[0]:03d}{path.suffix}"
        info = struct.pack("<qqqi", 2, path.stat().st_size, 133000000000000000 + n[0], len(str(path)) + 1)
        (sid / f"$I{name}").write_bytes(info + (str(path) + "\0").encode("utf-16-le"))
        os.rename(path, sid / f"$R{name}")

    monkeypatch.setattr(library, "send_to_recycle_bin", send)
    monkeypatch.setattr(library, "bin_root_of", lambda original: bin_root)
    real_find = library.find_in_recycle_bin
    monkeypatch.setattr(library, "find_in_recycle_bin", lambda p, b=None: real_find(p, bin_root))
    real_restore = library.restore_from_recycle_bin
    monkeypatch.setattr(library, "restore_from_recycle_bin", lambda p, b=None: real_restore(p, bin_root))
    return sid


def test_delete_goes_to_recycle_bin(dl, fake_bin):
    svc = service.Service()
    victim = next(i for i in svc.library_items({})["items"] if i["name"] == "MID_01.jpg")
    res = svc.library_delete([victim["id"]])
    assert res["ok"] and res["deleted"] == 1
    assert not Path(victim["path"]).exists() and len(list(fake_bin.glob("$R*"))) == 1
    assert svc.library_items({})["total"] == 3
    (gone,) = svc.library_deleted()["items"]
    assert gone["name"] == victim["name"] and gone["in_recycle_bin"]


def test_restore_puts_it_back_with_its_favorite(dl, fake_bin):
    svc = service.Service()
    victim = svc.library_items({})["items"][0]
    svc.library_set_favorite([victim["id"]], True)
    svc.library_delete([victim["id"]])
    res = svc.library_restore([victim["id"]])
    assert res["ok"] and res["restored"] == 1
    assert Path(victim["path"]).is_file() and not list(fake_bin.iterdir())
    assert svc.library_deleted()["items"] == []
    back = svc.library_items({"favorites": True})["items"]
    assert [i["id"] for i in back] == [victim["id"]]


def test_restored_by_hand_from_windows_is_noticed(dl, fake_bin):
    svc = service.Service()
    victim = svc.library_items({})["items"][0]
    svc.library_delete([victim["id"]])
    (r_file,) = fake_bin.glob("$R*")
    os.rename(r_file, victim["path"])                      # user used Explorer's "Restore"
    assert service.Service().library_items({})["total"] == 4
    assert svc.library_deleted()["items"] == []


def test_deleted_song_keeps_its_youtube_id_and_comes_back(dl, fake_bin, monkeypatch):
    # pulido P3 #26: «Volver a bajar» in Eliminadas, even with the Recycle Bin emptied
    from bakin import tools
    svc = service.Service()
    victim = svc.library_items({})["items"][0]
    monkeypatch.setattr(svc, "_lib_index", lambda real=svc._lib_index: _with_yt(real(), victim["id"]))
    svc.library_delete([victim["id"]])
    (gone,) = svc.library_deleted()["items"]
    assert gone["yt_id"] == "dQw4w9WgXcQ"
    seen = {}
    monkeypatch.setattr(tools, "missing", lambda: [])
    monkeypatch.setattr(svc, "_start_media", lambda url, opts, **kw: seen.update(url=url, **kw) or {"ok": True})
    monkeypatch.setattr(svc, "_push_queue", lambda: None)
    res = svc.library_redownload([victim["id"]])
    assert res["queued"] == 1 and seen["url"].endswith("v=dQw4w9WgXcQ")
    assert seen["preset_obj"]["folder"] == str(Path(victim["path"]).parent)
    assert seen["preset_obj"]["archive"] is False
    assert svc.library_deleted()["items"] == []


def _with_yt(idx, item_id):
    for items in idx["items"].values():
        for it in items:
            if it["id"] == item_id:
                it["yt_id"] = "dQw4w9WgXcQ"
    return idx


def test_emptied_bin_restore_fails_cleanly_and_can_be_forgotten(dl, fake_bin):
    svc = service.Service()
    victim = svc.library_items({})["items"][0]
    svc.library_delete([victim["id"]])
    for f in fake_bin.iterdir():
        f.unlink()                                        # Recycle Bin emptied
    res = svc.library_restore([victim["id"]])
    assert not res["ok"] and "Papelera" in res["failed"][0]["reason"]
    assert svc.library_deleted()["items"][0]["in_recycle_bin"] is False
    svc.library_forget([victim["id"]])
    assert svc.library_deleted()["items"] == []


def test_no_recycle_bin_means_no_delete(tmp_path):
    with pytest.raises(library.RecycleError):
        library.send_to_recycle_bin(Path("\\\\server\\share\\x.jpg"))


def test_recycle_bin_info_parser_v1_and_v2(tmp_path):
    sid = tmp_path / "bin" / "S-1"
    sid.mkdir(parents=True)
    original = "C:\\Fotos\\a.jpg"
    (sid / "$IAAA.jpg").write_bytes(struct.pack("<qqq", 1, 10, 5) + original.encode("utf-16-le").ljust(520, b"\0"))
    (sid / "$RAAA.jpg").write_bytes(b"old")
    (sid / "$IBBB.jpg").write_bytes(struct.pack("<qqqi", 2, 10, 9, len(original) + 1)
                                    + (original + "\0").encode("utf-16-le"))
    (sid / "$RBBB.jpg").write_bytes(b"new")
    found = library.find_in_recycle_bin(Path(original), tmp_path / "bin")
    assert found.name == "$RBBB.jpg"   # the most recent deletion wins


# ------------------------------------------------------------ «Fondos» filters

def test_query_orientation_resolution_and_color(dl, mine, tmp_path):
    from PIL import Image
    from bakin import imageinfo
    svc = service.Service()
    svc.library_add_folder(str(mine))
    wide = mine / "ancho.png"
    Image.new("RGB", (2560, 1440), (0, 102, 204)).save(wide)       # 0066cc, landscape, 1440p
    Image.new("RGB", (900, 900), (255, 255, 255)).save(mine / "cuadrada.png")
    svc._lib_invalidate()  # scan again with the two new images
    names = lambda f: sorted(i["name"] for i in svc.library_items(f, 0, 100)["items"])  # noqa: E731
    # the size is read while scanning; the dl images are 4:5, portrait
    assert names({"orient": "landscape"}) == ["ancho.png", "playa.png"]
    assert names({"orient": "square"}) == ["cuadrada.png"]
    assert "NEW_01.jpg" in names({"orient": "portrait"}) and "clip.mp4" not in names({"orient": "portrait"})
    assert names({"min_px": 1440}) == ["NEW_01.jpg", "NEW_02.jpg", "ancho.png"]
    # the colour is worked out in the background
    if svc._lib_cache:
        for _ in range(200):
            if not getattr(svc, "_colors_busy", False):
                break
            import time
            time.sleep(0.05)
    assert names({"color": "0066cc"}) == ["ancho.png"]
    assert names({"color": "ffffff", "orient": "square"}) == ["cuadrada.png"]
    # an item's size is shown on its tile
    it = next(i for i in svc.library_items({"orient": "landscape"}, 0, 10)["items"] if i["name"] == "ancho.png")
    assert (it["width"], it["height"]) == (2560, 1440)
    assert imageinfo.orientation(1000, 1030) == "square"


def test_wallhaven_colours_are_remembered(tmp_path):
    from bakin import imageinfo
    f = tmp_path / "w.jpg"
    f.write_bytes(jpeg_bytes(40, 30))
    st = f.stat()
    imageinfo.remember(f, 3840, 2160, ["#663399", "#000000"])
    assert imageinfo.color(str(f), st.st_size, int(st.st_mtime)) == "663399"
    assert imageinfo.dims(str(f), st.st_size, int(st.st_mtime)) == (3840, 2160)
    imageinfo.remember(f, 0, 0, ["#123456"])  # not in the palette: the closest one
    assert imageinfo.color(str(f), st.st_size, int(st.st_mtime)) in imageinfo.PALETTE


def test_a_downloading_songs_cover_is_not_a_photo(tmp_path, monkeypatch):
    # 0.13 review: while yt-dlp downloads a song, its cover sits next to it as an image and
    # showed in Fotos until the download finished
    root = tmp_path / "dl"
    music = root / "Música"
    music.mkdir(parents=True)
    (music / ".bakin-media.json").write_text('{"preset": "Música"}', encoding="utf-8")
    (music / "Tema.webp").write_bytes(jpeg_bytes(40, 40))         # the cover, while it downloads
    (music / "Tema.f251.webm.part").write_bytes(b"\0" * 100)
    (music / "Otro.jpg").write_bytes(jpeg_bytes(40, 40))          # a finished song's cover
    (music / "Otro.opus").write_bytes(b"\0" * 100)
    (music / "Tema renombrado [dQw4w9WgXcQ].webp").write_bytes(jpeg_bytes(40, 40))  # left behind (2026-11-07)
    walls = root / "Wallhaven"
    walls.mkdir()
    (walls / ".bakin-media.json").write_text('{"preset": "Wallhaven"}', encoding="utf-8")
    (walls / "wallhaven-abc123.jpg").write_bytes(jpeg_bytes(40, 40))
    service.save_settings({**service.load_settings(), "out_dir": str(root)})
    names = sorted(i["name"] for i in service.Service().library_items({"kinds": ["image"]}, 0, 50)["items"])
    assert names == ["wallhaven-abc123.jpg"]  # a music folder's pictures are covers, all of them
    # in the user's own folders nothing is hidden
    mine = tmp_path / "Mis cosas"
    mine.mkdir()
    (mine / "tapa.jpg").write_bytes(jpeg_bytes(40, 40))
    (mine / "tapa.mp3").write_bytes(b"\0" * 100)
    svc = service.Service()
    svc.library_add_folder(str(mine))
    assert "tapa.jpg" in [i["name"] for i in svc.library_items({"kinds": ["image"]}, 0, 50)["items"]]


def test_music_sorts_both_ways_with_empty_values_last():
    # a friend (2026-11-07): sort by date added, and every column both ways, like Spotify
    from bakin import library
    def it(n, title, artist, dur, date):
        return {"id": n, "rel": n, "name": f"{n}.opus", "kind": "audio", "fav": False, "tags": [], "size": 1,
                "title": title, "artist": artist, "album": "", "duration": dur, "date": date}
    items = [it("a", "Brisa", "Zeta", 200, 30), it("b", "Alba", "", 90, 10), it("c", "", "Mora", None, 20)]
    ids = lambda sort: [i["id"] for i in library.query(items, {"sort": sort})]
    assert ids("new") == ["a", "c", "b"] and ids("old") == ["b", "c", "a"]
    assert ids("title") == ["b", "a", "c"] and ids("title_desc") == ["a", "b", "c"]     # no title: last, both ways
    assert ids("artist") == ["c", "a", "b"] and ids("artist_desc") == ["a", "c", "b"]
    assert ids("duration_asc") == ["b", "a", "c"] and ids("duration") == ["a", "b", "c"]  # no length: last
