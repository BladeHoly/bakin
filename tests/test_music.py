"""Music library: tags, sorting, low-quality filter, and "better copy" upgrades."""

import os
import subprocess
from pathlib import Path

import pytest

from bakin import audiotags, library, mediadl, service, tools

REAL_FFMPEG = Path(os.environ.get("LOCALAPPDATA", "")) / "BakinData" / "tools" / "ffmpeg" / "ffmpeg.exe"
needs_ffmpeg = pytest.mark.skipif(not REAL_FFMPEG.is_file(), reason="needs ffmpeg installed by Bakin")


def make(dest, codec, title, artist, album="", seconds=2, comment=""):
    args = [str(REAL_FFMPEG), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
            "-i", f"sine=frequency=500:duration={seconds}"]
    args += {"mp3": ["-c:a", "libmp3lame", "-b:a", "128k"], "opus": ["-c:a", "libopus", "-b:a", "160k"],
             "flac": ["-c:a", "flac"]}[codec]
    for k, v in (("title", title), ("artist", artist), ("album", album), ("comment", comment)):
        if v:
            args += ["-metadata", f"{k}={v}"]
    subprocess.run(args + [str(dest)], check=True, capture_output=True, creationflags=0x08000000)
    return dest


@pytest.fixture
def music(tmp_path):
    audiotags.reset_memory_cache()
    d = tmp_path / "Musica"
    d.mkdir()
    make(d / "Zeta - Viejo [aaaaaaaaaaa].mp3", "mp3", "Viejo", "Zeta", "Uno")
    make(d / "sin id.mp3", "mp3", "Suelto", "Alfa", comment="https://www.youtube.com/watch?v=bbbbbbbbbbb")
    make(d / "Nuevo [ccccccccccc].opus", "opus", "Nuevo", "Beta", "Dos")
    make(d / "Perfecto.flac", "flac", "Perfecto", "Gamma")
    (d / "notas.mp3").write_bytes(b"not audio")
    return d


@needs_ffmpeg
def test_tags_codec_bitrate_and_youtube_id(music):
    m = audiotags.read(music / "Zeta - Viejo [aaaaaaaaaaa].mp3")
    assert (m["title"], m["artist"], m["album"], m["codec"], m["yt_id"]) == ("Viejo", "Zeta", "Uno", "mp3", "aaaaaaaaaaa")
    assert 120 <= m["bitrate"] <= 136 and 1.5 < m["duration"] < 2.6 and audiotags.is_low_quality(m)
    assert audiotags.read(music / "sin id.mp3")["yt_id"] == "bbbbbbbbbbb"       # from the comment tag
    o = audiotags.read(music / "Nuevo [ccccccccccc].opus")
    assert o["codec"] == "opus" and not audiotags.is_low_quality(o)
    f = audiotags.read(music / "Perfecto.flac")
    assert f["lossless"] and not audiotags.is_low_quality(f)
    junk = audiotags.read(music / "notas.mp3")
    assert junk["title"] == "" and junk["duration"] is None                      # unreadable: no crash


@needs_ffmpeg
def test_library_sorts_and_filters_by_tags(music, monkeypatch):
    svc = service.Service()
    assert svc.library_add_folder(str(music))["ok"]
    by_artist = svc.library_items({"kinds": ["audio"], "sort": "artist"})["items"]
    assert [i["artist"] for i in by_artist][:4] == ["Alfa", "Beta", "Gamma", "Zeta"]
    low = svc.library_items({"kinds": ["audio"], "low_quality": True})["items"]
    assert sorted(i["name"] for i in low) == ["Zeta - Viejo [aaaaaaaaaaa].mp3", "notas.mp3", "sin id.mp3"]
    assert svc.library_items({"q": "zeta"})["total"] == 1                         # search hits the artist tag
    assert (Path(os.environ["BAKIN_HOME"]) / "library" / "audio-tags.json").is_file()   # cached


@needs_ffmpeg
def test_upgrade_queues_better_copies_in_place(music, monkeypatch):
    monkeypatch.setattr(tools, "missing", lambda required=None: [])
    monkeypatch.setattr(service.Service, "_ensure_worker", lambda self: None)
    svc = service.Service()
    svc.library_add_folder(str(music))
    items = {i["name"]: i["id"] for i in svc.library_items({"kinds": ["audio"]}, 0, 50)["items"]}
    r = svc.library_upgrade_audio(list(items.values()), "", "opus")
    assert r["ok"] and r["queued"] == 2                                           # the two MP3s with an id
    assert "Nuevo [ccccccccccc].opus" in r["already"] and "Perfecto.flac" in r["already"]
    assert r["no_id"] == ["notas.mp3"]
    job = next(q for q in svc._queue if "aaaaaaaaaaa" in q["url"])
    p = job["preset"]
    assert (p["audio_format"], p["archive"], p["folder"]) == ("opus", False, str(music))
    assert p["template"] == "Zeta - Viejo [aaaaaaaaaaa].%(ext)s"
    assert job["replaces"]["path"].endswith("Zeta - Viejo [aaaaaaaaaaa].mp3")
    args = mediadl.build_args(p, job["url"], music, with_cookies=False)
    assert "--download-archive" not in args


@needs_ffmpeg
def test_finished_upgrade_moves_tags_and_recycles_old(music, monkeypatch):
    sent = []
    monkeypatch.setattr(library, "send_to_recycle_bin", lambda path: sent.append(path) or os.remove(path))
    svc = service.Service()
    svc.library_add_folder(str(music))
    old = next(i for i in svc.library_items({"kinds": ["audio"]}, 0, 50)["items"] if i["name"].startswith("Zeta"))
    svc.library_set_favorite([old["id"]], True)
    svc.library_tag([old["id"]], add=["rock"])
    new = make(music / "Zeta - Viejo [aaaaaaaaaaa].opus", "opus", "Viejo", "Zeta")

    class Stats:
        files = [str(new)]
    svc._finish_upgrade({"id": old["id"], "path": old["path"], "yt_id": "aaaaaaaaaaa"}, Stats())
    assert [p.name for p in sent] == ["Zeta - Viejo [aaaaaaaaaaa].mp3"]
    svc._lib_invalidate()
    fresh = {i["name"]: i for i in svc.library_items({"kinds": ["audio"]}, 0, 50)["items"]}
    assert fresh["Zeta - Viejo [aaaaaaaaaaa].opus"]["fav"] and fresh["Zeta - Viejo [aaaaaaaaaaa].opus"]["tags"] == ["rock"]
    assert any(d["name"].endswith(".mp3") for d in svc.library_deleted()["items"])


@needs_ffmpeg
def test_failed_upgrade_keeps_the_old_file(music):
    events = []
    svc = service.Service(events.append)
    svc.library_add_folder(str(music))
    old = next(i for i in svc.library_items({"kinds": ["audio"]}, 0, 50)["items"] if i["name"].startswith("Zeta"))

    class Stats:
        files = []
    svc._finish_upgrade({"id": old["id"], "path": old["path"], "yt_id": "aaaaaaaaaaa"}, Stats())
    assert Path(old["path"]).is_file() and "se conserva" in events[-1]["message"]


def test_private_playlist_errors_trigger_auto_cookies():
    assert mediadl.AUTH_ERRORS.search("ERROR: [youtube:tab] LM: The playlist does not exist.")
    assert mediadl.AUTH_ERRORS.search("This playlist type is unviewable")


def test_preset_folders_join_the_library_once(tmp_path, monkeypatch):
    music = tmp_path / "Music"
    music.mkdir()
    dl = tmp_path / "dl"
    dl.mkdir()
    service.save_settings({**service.load_settings(), "out_dir": str(dl)})
    s = mediadl.load_settings()
    for p in s["presets"]:
        if p["mode"] == "audio":
            p["folder"] = str(music)
    mediadl.save_settings(s)
    svc = service.Service()
    ov = svc.library_overview()
    assert [c["path"] for c in ov["collections"] if c["kind"] == "folder"] == [str(music.resolve())]
    col = next(c for c in ov["collections"] if c["kind"] == "folder")
    svc.library_remove_folder(col["id"])
    assert not [c for c in svc.library_overview()["collections"] if c["kind"] == "folder"]   # stays removed


@needs_ffmpeg
def test_embedded_cover_becomes_a_square_thumb(music, tmp_path, monkeypatch):
    import base64
    import io

    from mutagen.flac import Picture
    from mutagen.id3 import APIC, ID3
    from mutagen.oggopus import OggOpus
    from PIL import Image
    monkeypatch.setenv("BAKIN_HOME", str(tmp_path / "home"))
    buf = io.BytesIO()
    Image.new("RGB", (320, 180), (200, 30, 30)).save(buf, "JPEG")  # 16:9 like YouTube's
    art = buf.getvalue()
    mp3 = music / "Zeta - Viejo [aaaaaaaaaaa].mp3"
    tags = ID3(mp3)
    tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="", data=art))
    tags.save(mp3)
    opus = music / "Nuevo [ccccccccccc].opus"
    f = OggOpus(opus)
    pic = Picture()
    pic.type, pic.mime, pic.data = 3, "image/jpeg", art
    f["metadata_block_picture"] = [base64.b64encode(pic.write()).decode()]
    f.save()
    assert audiotags.cover(mp3) == art and audiotags.cover(opus) == art
    assert audiotags.cover(music / "Perfecto.flac") is None and audiotags.cover(music / "notas.mp3") is None
    st = opus.stat()
    uri = library.thumb({"path": str(opus), "ext": "opus", "kind": "audio", "size": st.st_size, "mtime": int(st.st_mtime)}, 320)
    with Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1]))) as im:
        assert im.size == (180, 180)  # 16:9 cut to its square middle
    st = (music / "Perfecto.flac").stat()
    assert library.thumb({"path": str(music / "Perfecto.flac"), "ext": "flac", "kind": "audio",
                          "size": st.st_size, "mtime": int(st.st_mtime)}) == ""


def test_playlist_entries_get_youtube_thumbnails_only():
    assert mediadl.entry_thumb({"id": "Ip_Kc3EQiOI", "ie_key": "Youtube"}) == "https://i.ytimg.com/vi/Ip_Kc3EQiOI/mqdefault.jpg"
    assert mediadl.entry_thumb({"id": "123456", "ie_key": "Soundcloud"}) == ""
    assert mediadl.entry_thumb({"id": "../evil/x?y", "ie_key": "Youtube"}) == ""
