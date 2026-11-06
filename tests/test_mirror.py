"""Playlists espejo: a folder kept equal to a playlist."""

import json
import os
from pathlib import Path

import pytest

from bakin import mirror
from bakin.mirror import MirrorError


class FakeTrash:
    """Records what would go to the Recycle Bin and moves it aside (never the real one)."""

    def __init__(self, bin_dir: Path):
        self.bin = bin_dir
        self.bin.mkdir(parents=True, exist_ok=True)
        self.calls: list[list[Path]] = []

    def __call__(self, paths):
        self.calls.append(list(paths))
        for p in paths:
            os.replace(p, self.bin / f"{len(self.calls)}-{p.name}")

    @property
    def all(self):
        return [p for c in self.calls for p in c]


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "Music"
    root.mkdir()
    return root


@pytest.fixture
def trash(tmp_path):
    return FakeTrash(tmp_path / "bin")


def song(root: Path, rel: str, data: bytes = b"x" * 100, title=None, artist=None, sid=None, **kw):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_bytes(data)
    st = p.stat()
    return {"id": sid or rel, "path": str(p), "name": p.name, "size": st.st_size,
            "mtime": st.st_mtime, "title": title, "artist": artist, **kw}


def sync(songs, folder, trash, lib, **kw):
    apply_kw = {k: kw.pop(k) for k in ("progress", "cancel") if k in kw}
    p = mirror.plan(songs, folder, forbidden=[lib], **kw)
    return p, mirror.apply(p, folder, trash, **apply_kw)


def manifest(folder):
    return json.loads((folder / mirror.MANIFEST).read_text(encoding="utf-8"))


def test_add_then_second_plan_is_empty(tmp_path, lib, trash):
    out = tmp_path / "stick"
    songs = [song(lib, "a.mp3", title="A", artist="X"), song(lib, "b.flac")]
    p, r = sync(songs, out, trash, lib)
    assert [Path(j["dst"]).name for j in p["copy"]] == ["a.mp3", "b.flac"]
    assert r == {"copied": 2, "updated": 0, "removed": 0, "failed": [], "cancelled": False}
    assert (out / "a.mp3").read_bytes() == b"x" * 100
    # copy2 kept the time
    assert abs((out / "a.mp3").stat().st_mtime - songs[0]["mtime"]) < 0.01
    again = mirror.plan(songs, out, forbidden=[lib])
    assert again["copy"] == again["update"] == again["remove"] == []
    assert again["keep"] == 2 and not again["m3u8_changed"]
    assert mirror.is_empty(again)
    assert trash.calls == []


def test_update_when_size_differs_or_older(tmp_path, lib, trash):
    out = tmp_path / "stick"
    songs = [song(lib, "a.mp3"), song(lib, "b.mp3")]
    sync(songs, out, trash, lib)
    (lib / "a.mp3").write_bytes(b"y" * 150)  # bigger
    t = songs[1]["mtime"] + 100
    os.utime(lib / "b.mp3", (t, t))  # same size, newer source
    songs = [song(lib, "a.mp3"), song(lib, "b.mp3")]
    p, r = sync(songs, out, trash, lib)
    assert p["copy"] == [] and len(p["update"]) == 2
    assert r["updated"] == 2
    assert (out / "a.mp3").read_bytes() == b"y" * 150
    assert mirror.is_empty(mirror.plan(songs, out, forbidden=[lib]))


def test_remove_only_what_bakin_put_there(tmp_path, lib, trash):
    out = tmp_path / "stick"
    a, b, c = song(lib, "a.mp3"), song(lib, "b.mp3"), song(lib, "c.mp3")
    sync([a, b, c], out, trash, lib)
    (out / "mine.mp3").write_bytes(b"user's own")  # the user's, never touched
    (out / "notes.txt").write_text("hola")
    assert mirror.first_removal(out)
    p, r = sync([a, c], out, trash, lib)
    assert p["remove"] == [str(out / "b.mp3")]
    assert p["first_removal"] is True
    assert trash.all == [out / "b.mp3"]
    assert r["removed"] == 1 and not (out / "b.mp3").exists()
    assert (out / "mine.mp3").read_bytes() == b"user's own"
    assert (out / "notes.txt").exists()
    assert not mirror.first_removal(out)
    assert set(manifest(out)["files"]) == {"a.mp3", "c.mp3"}
    # emptying the playlist still leaves the user's files
    p, r = sync([], out, trash, lib)
    assert sorted(Path(x).name for x in p["remove"]) == ["a.mp3", "c.mp3"]
    assert (out / "mine.mp3").exists() and (out / "notes.txt").exists()


def test_trash_failure_keeps_file_in_manifest(tmp_path, lib):
    out = tmp_path / "stick"
    a, b = song(lib, "a.mp3"), song(lib, "b.mp3")
    sync([a, b], out, lambda ps: None, lib)

    def refuse(paths):
        raise RuntimeError("esta unidad no tiene Papelera de reciclaje; no se borró")

    p, r = sync([a], out, refuse, lib)
    assert r["removed"] == 0
    assert r["failed"] == [{"file": str(out / "b.mp3"),
                            "error": "esta unidad no tiene Papelera de reciclaje; no se borró"}]
    assert "b.mp3" in manifest(out)["files"]
    assert mirror.first_removal(out)
    # and it's offered again next time
    assert mirror.plan([a], out, forbidden=[lib])["remove"] == [str(out / "b.mp3")]


def test_names_colliding(tmp_path, lib, trash):
    out = tmp_path / "stick"
    songs = [song(lib, "x/Song.mp3", b"1"), song(lib, "y/song.mp3", b"22"), song(lib, "z/Song.mp3", b"333")]
    p, r = sync(songs, out, trash, lib)
    assert [Path(j["dst"]).name for j in p["copy"]] == ["Song.mp3", "song (2).mp3", "Song (3).mp3"]
    assert (out / "song (2).mp3").read_bytes() == b"22"
    assert (out / "Song (3).mp3").read_bytes() == b"333"
    # reordering keeps the names (nothing copied again)
    p2 = mirror.plan(list(reversed(songs)), out, forbidden=[lib])
    assert p2["copy"] == p2["update"] == p2["remove"] == []
    assert p2["m3u8_changed"]
    # a user file with the same name pushes ours aside
    out2 = tmp_path / "phone"
    out2.mkdir()
    (out2 / "Song.mp3").write_bytes(b"user")
    p3, _ = sync(songs[:1], out2, trash, lib, adopt=True)
    assert Path(p3["copy"][0]["dst"]).name == "Song (2).mp3"
    assert (out2 / "Song.mp3").read_bytes() == b"user"


def test_m3u8_content(tmp_path, lib, trash):
    out = tmp_path / "stick"
    songs = [song(lib, "Ñandú.mp3", title="Canción", artist="Él", duration=215.7),
             song(lib, "b.mp3"), song(lib, "c/b.mp3", b"zz", title="Solo título")]
    sync(songs, out, trash, lib)
    raw = (out / "playlist.m3u8").read_bytes()
    assert raw.decode("utf-8") == (
        "#EXTM3U\n"
        "#EXTINF:215,Él - Canción\nÑandú.mp3\n"
        "#EXTINF:-1,b\nb.mp3\n"
        "#EXTINF:-1,Solo título\nb (2).mp3\n")
    assert "playlist.m3u8" not in manifest(out)["files"]  # not a song: never trashed
    assert manifest(out)["m3u8"] == "playlist.m3u8"


def test_users_playlist_m3u8_not_overwritten(tmp_path, lib, trash):
    out = tmp_path / "phone"
    out.mkdir()
    (out / "playlist.m3u8").write_text("mine", encoding="utf-8")
    p, _ = sync([song(lib, "a.mp3")], out, trash, lib, adopt=True)
    assert p["m3u8_name"] == "playlist (Bakin).m3u8"
    assert (out / "playlist.m3u8").read_text(encoding="utf-8") == "mine"
    assert (out / "playlist (Bakin).m3u8").exists()
    assert mirror.is_empty(mirror.plan([song(lib, "a.mp3")], out, forbidden=[lib]))


def test_hardlink_mode(tmp_path, lib, trash):
    out = tmp_path / "linked"
    songs = [song(lib, "a.mp3")]
    sync(songs, out, trash, lib, mode="hardlink")
    assert os.path.samefile(out / "a.mp3", lib / "a.mp3")
    assert mirror.is_empty(mirror.plan(songs, out, mode="hardlink", forbidden=[lib]))


def test_hardlink_falls_back_to_copy(tmp_path, lib, trash, monkeypatch):
    def cross_drive(src, dst):
        raise OSError(17, "Cannot create a link to a file on a different drive")

    monkeypatch.setattr(mirror.os, "link", cross_drive)
    out = tmp_path / "other"
    p, r = sync([song(lib, "a.mp3")], out, trash, lib, mode="hardlink")
    assert r["copied"] == 1 and r["failed"] == []
    assert not os.path.samefile(out / "a.mp3", lib / "a.mp3")
    assert (out / "a.mp3").read_bytes() == b"x" * 100
    assert not [f for f in out.iterdir() if f.suffix in (".part", ".tmp")]


def test_forbidden_folders(tmp_path, lib):
    songs = [song(lib, "a.mp3")]
    with pytest.raises(MirrorError, match="dentro de la biblioteca"):
        mirror.plan(songs, lib / "espejo", forbidden=[lib])
    with pytest.raises(MirrorError, match="contiene la biblioteca"):
        mirror.plan(songs, tmp_path, forbidden=[lib])
    with pytest.raises(MirrorError):
        mirror.plan(songs, lib, forbidden=[lib])
    # a song from outside the library roots, under the chosen folder
    other = tmp_path / "loose"
    s = song(other, "sub/z.mp3")
    with pytest.raises(MirrorError, match="contiene canciones"):
        mirror.plan([s], other, forbidden=[lib])
    with pytest.raises(MirrorError):
        mirror.plan(songs, tmp_path / "x", mode="move", forbidden=[lib])


def test_non_empty_folder_needs_adopt(tmp_path, lib, trash):
    out = tmp_path / "phone"
    out.mkdir()
    (out / "old.mp3").write_bytes(b"old")
    songs = [song(lib, "a.mp3")]
    with pytest.raises(MirrorError, match="no está vacía"):
        mirror.plan(songs, out, forbidden=[lib])
    p, r = sync(songs, out, trash, lib, adopt=True)
    assert p["adopted"] == ["old.mp3"] and p["remove"] == []
    assert manifest(out)["adopted"] == ["old.mp3"]
    # adopted files are never removed, even after the playlist empties
    p, r = sync([], out, trash, lib)
    assert p["remove"] == [str(out / "a.mp3")]
    assert (out / "old.mp3").read_bytes() == b"old"
    # a folder with a manifest no longer needs adopt
    mirror.plan(songs, out, forbidden=[lib])


def test_cancel_midway_keeps_manifest_consistent(tmp_path, lib, trash):
    out = tmp_path / "stick"
    songs = [song(lib, f"{n}.mp3") for n in "abcde"]
    seen = []

    def progress(done, total, name):
        seen.append(name)

    p, r = sync(songs, out, trash, lib, progress=progress, cancel=lambda: len(seen) >= 2)
    assert r["cancelled"] and r["copied"] == 2
    there = {f.name for f in out.iterdir() if f.suffix == ".mp3"}
    assert there == {"a.mp3", "b.mp3"} == set(manifest(out)["files"])
    assert (out / "playlist.m3u8").read_text(encoding="utf-8").count(".mp3") == 2
    assert not [f for f in out.iterdir() if f.suffix in (".part", ".tmp")]
    # the next sync picks up where it stopped
    p2, r2 = sync(songs, out, trash, lib)
    assert [Path(j["dst"]).name for j in p2["copy"]] == ["c.mp3", "d.mp3", "e.mp3"]
    assert p2["keep"] == 2 and r2["copied"] == 3
    assert mirror.is_empty(mirror.plan(songs, out, forbidden=[lib]))


def test_copy_failure_reported(tmp_path, lib, trash):
    out = tmp_path / "stick"
    a, b = song(lib, "a.mp3"), song(lib, "b.mp3")
    p = mirror.plan([a, b], out, forbidden=[lib])
    (lib / "a.mp3").unlink()  # gone between plan and apply
    r = mirror.apply(p, out, trash)
    assert r["copied"] == 1 and len(r["failed"]) == 1
    assert r["failed"][0]["file"] == a["path"]
    assert set(manifest(out)["files"]) == {"b.mp3"}
    assert (out / "playlist.m3u8").read_text(encoding="utf-8").count(".mp3") == 1


def test_file_deleted_by_hand_is_copied_again(tmp_path, lib, trash):
    out = tmp_path / "stick"
    songs = [song(lib, "a.mp3")]
    sync(songs, out, trash, lib)
    (out / "a.mp3").unlink()
    p, r = sync(songs, out, trash, lib)
    assert len(p["copy"]) == 1 and r["copied"] == 1 and trash.calls == []
