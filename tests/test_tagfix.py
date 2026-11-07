"""Confirmed tag changes and their undo (bakin.tagfix), with audiotags faked: no audio files, no
ffmpeg. Covers which songs are skipped or fail, the backup log and putting covers back."""

import json

import pytest

from bakin import audiotags, tagfix


class FakeTags:
    """Tags and covers in a dict keyed by path; paths in `locked` act like a file in use."""

    def __init__(self):
        self.tags: dict[str, dict] = {}
        self.covers: dict[str, bytes | None] = {}
        self.locked: set[str] = set()

    def snapshot(self, path):
        if str(path) not in self.tags:
            raise OSError("not audio")
        return dict(self.tags[str(path)])

    def write(self, path, **fields):
        if str(path) in self.locked:
            raise PermissionError("in use")
        self.tags[str(path)].update(fields)

    def cover(self, path):
        return self.covers.get(str(path))

    def write_cover(self, path, image):
        self.covers[str(path)] = image


@pytest.fixture
def tags(monkeypatch, tmp_path):
    fake = FakeTags()
    for name in ("snapshot", "write", "cover", "write_cover"):
        monkeypatch.setattr(audiotags, name, getattr(fake, name))
    monkeypatch.setattr(audiotags, "forget", lambda path: None)
    for n in ("a", "b", "c"):
        fake.tags[str(tmp_path / f"{n}.opus")] = {"title": n.upper(), "artist": "X", "album": "", "year": ""}
    return fake


def test_apply_skips_the_playing_song_and_reports_failures(tags, tmp_path):
    a, b, c = (str(tmp_path / f"{n}.opus") for n in "abc")
    tags.locked.add(b)
    r = tagfix.apply([{"id": 1, "path": a, "title": "Nuevo", "artist": None},
                      {"id": 2, "path": b, "title": "Otro"},
                      {"id": 3, "path": c, "title": "Tercero"},
                      {"id": 4, "path": str(tmp_path / "gone.opus"), "title": "?"}],
                     skip_paths={c})
    assert r["done"] == [1] and r["skipped"] == [3] and r["batch"]
    failed = {f["id"]: f["error"] for f in r["failed"]}
    assert "en uso" in failed[2] and failed[4] == "not audio"
    assert tags.tags[a]["title"] == "Nuevo" and tags.tags[a]["artist"] == "X"  # None means «leave it»


def test_nothing_done_means_no_batch(tags, tmp_path):
    r = tagfix.apply([{"id": 1, "path": str(tmp_path / "a.opus")}], skip_paths={str(tmp_path / "a.opus")})
    assert r == {"batch": None, "done": [], "failed": [], "skipped": [1]}
    assert tagfix.last_batch() is None


def test_undo_puts_tags_and_covers_back(tags, tmp_path):
    a, b = str(tmp_path / "a.opus"), str(tmp_path / "b.opus")
    tags.covers[a] = b"old cover"
    first = tagfix.apply([{"id": 1, "path": b, "title": "B2"}])["batch"]
    second = tagfix.apply([{"id": 1, "path": a, "title": "A2", "cover": b"new cover"},
                           {"id": 2, "path": b, "album": "Disco", "cover": b"new cover"}])["batch"]
    assert tags.covers[b] == b"new cover"
    assert tagfix.last_batch()["batch"] == second and tagfix.last_batch()["count"] == 2
    assert tagfix.undo() == {"done": [2, 1], "failed": []}
    assert tags.tags[a]["title"] == "A" and tags.covers[a] == b"old cover"
    assert tags.tags[b]["album"] == "" and tags.covers[b] is None  # it had none: removed again
    assert tags.tags[b]["title"] == "B2"  # the earlier batch stays
    assert tagfix.last_batch()["batch"] == first  # the one undone isn't offered again
    tagfix.undo()
    assert tags.tags[b]["title"] == "B" and tagfix.last_batch() is None


def test_undo_keeps_what_it_could_not_write(tags, tmp_path):
    a = str(tmp_path / "a.opus")
    tagfix.apply([{"id": 1, "path": a, "title": "A2"}])
    tags.locked.add(a)
    r = tagfix.undo()
    assert r["done"] == [] and "en uso" in r["failed"][0]["error"]
    tags.locked.clear()
    assert tagfix.undo()["done"] == [1]  # still there to try again


def test_a_damaged_backup_line_is_skipped(tags, tmp_path):
    tagfix.apply([{"id": 1, "path": str(tmp_path / "a.opus"), "title": "A2"}])
    log = tagfix._backup_file()
    log.write_text("{half a line\n" + log.read_text(encoding="utf-8"), encoding="utf-8")
    assert tagfix.last_batch()["count"] == 1
    assert tagfix.undo()["done"] == [1]
    assert all(json.loads(l).get("undone") for l in log.read_text(encoding="utf-8").splitlines())


def test_why_without_a_message():
    assert tagfix._why(ValueError()) == "ValueError"
