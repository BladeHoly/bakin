"""PLAN 0.18.2: versioned data files and their migrations."""

import json

import pytest

from bakin import migrate, mediadl, service
from bakin.paths import app_dir


def _put(rel: str, data) -> None:
    p = app_dir().joinpath(*rel.split("/"))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _get(rel: str):
    return json.loads(app_dir().joinpath(*rel.split("/")).read_text(encoding="utf-8"))


OLD_SETTINGS = {"out_dir": "D:\\Bajadas", "wallhaven_user": "someone", "musicbrainz": True}
OLD_MEDIA = {"presets": [{"name": "Mi música", "id": "mine", "folder": "Música", "mode": "audio",
                          "audio_format": "opus"},
                         {"name": "Sin id", "mode": "video"},   # an id-less preset: a new one on every load
                         "not a preset"],
             "default_preset": "mine", "builtins_seen": ["bimusic"], "daily_limit": 50}
OLD_QUEUE = {"items": [{"kind": "media", "title": "x", "mode": "download", "url": "https://soundcloud.com/a/b",
                        "preset": "mine", "root": "D:\\Bajadas", "retry_on_partial": False},
                       {"kind": "retired", "title": "@someone", "mode": "download", "target": "someone"}],
             "paused": True, "paused_until": 5e9, "reason": "límite", "scope": "media"}
OLD_FOLLOWS = {"follows": [{"id": "a1", "url": "https://example.com/someone/", "kind": "retired",
                            "title": "@someone", "every_hours": 24, "last_check": 0, "enabled": True},
                           {"id": "b2", "url": "https://soundcloud.com/a", "kind": "media", "preset": "mine",
                            "title": "soundcloud.com/a", "every_hours": 24, "last_check": 0, "enabled": True}]}


@pytest.fixture
def old_files():
    _put("ui-settings.json", OLD_SETTINGS)
    _put("media.json", OLD_MEDIA)
    _put("queue.json", OLD_QUEUE)
    _put("follows.json", OLD_FOLLOWS)


def test_registry_is_numbered_from_one():
    migrate._check_registry()
    for rel in migrate.MIGRATIONS:
        assert migrate.target_version(rel) >= 1


def test_nothing_to_do_without_files():
    assert migrate.run() == {}


def test_old_files_get_the_new_shape_and_a_version(old_files):
    changed = migrate.run()
    assert changed == {"ui-settings.json": (0, 1), "media.json": (0, 1), "queue.json": (0, 1),
                       "follows.json": (0, 1)}

    s = _get("ui-settings.json")
    assert s == {"_v": 1, **OLD_SETTINGS}

    m = _get("media.json")
    assert m["_v"] == 1 and m["daily_limit"] == 50 and m["default_preset"] == "mine"
    assert len(m["presets"]) == 2  # the entry that wasn't a preset is gone
    full = set(mediadl.new_preset("x"))
    assert all(set(p) == full for p in m["presets"])
    assert m["presets"][0]["id"] == "mine" and m["presets"][0]["audio_format"] == "opus"
    assert m["presets"][1]["id"]  # a stable id from now on

    q = _get("queue.json")
    assert q["_v"] == 1
    assert [i["kind"] for i in q["items"]] == ["media"]
    assert q["holds"] == {"media": {"until": 5e9, "reason": "límite"}}
    assert not set(q) & {"paused", "paused_until", "reason", "scope"}

    f = _get("follows.json")
    assert f["_v"] == 1 and [x["id"] for x in f["follows"]] == ["b2"]


def test_backup_is_written_once_and_kept(old_files):
    migrate.run()
    backup = app_dir() / "queue.before-0.json"
    assert json.loads(backup.read_text(encoding="utf-8")) == OLD_QUEUE
    for rel in ("ui-settings", "media", "follows"):
        assert (app_dir() / f"{rel}.before-0.json").is_file()
    # a writer that drops "_v" makes the file version 0 again: it's stamped
    # again, but the first backup (the real original) is kept
    _put("queue.json", {"items": [], "holds": {}})
    assert migrate.run() == {"queue.json": (0, 1)}
    assert json.loads(backup.read_text(encoding="utf-8")) == OLD_QUEUE


def test_second_run_changes_nothing(old_files):
    migrate.run()
    before = {p.name: p.read_bytes() for p in app_dir().iterdir() if p.is_file()}
    assert migrate.run() == {}
    after = {p.name: p.read_bytes() for p in app_dir().iterdir() if p.is_file()}
    assert after == before


@pytest.mark.parametrize("content", ["{not json", "[1, 2, 3]", '{"_v": "one", "x": 1}', '{"_v": 7, "x": 1}'])
def test_files_it_cannot_handle_are_left_alone(content):
    _put("media.json", content)
    assert migrate.run() == {}
    assert (app_dir() / "media.json").read_text(encoding="utf-8") == content
    assert not (app_dir() / "media.before-0.json").exists()


def test_a_failing_step_leaves_the_file_alone(monkeypatch):
    _put("follows.json", OLD_FOLLOWS)

    def boom(data):
        raise RuntimeError("nope")

    monkeypatch.setitem(migrate.MIGRATIONS, "follows.json", [(1, boom)])
    assert migrate.run() == {}
    assert _get("follows.json") == OLD_FOLLOWS
    assert not (app_dir() / "follows.before-0.json").exists()


def test_steps_run_in_order_from_the_files_version(monkeypatch):
    _put("sub/thing.json", {"_v": 1, "a": 1})
    monkeypatch.setitem(migrate.MIGRATIONS, "sub/thing.json",
                        [(1, lambda d: {**d, "one": True}), (2, lambda d: {**d, "two": d["a"] + 1}),
                         (3, lambda d: {**d, "three": d["two"] * 10})])
    assert migrate.run()["sub/thing.json"] == (1, 3)
    assert _get("sub/thing.json") == {"_v": 3, "a": 1, "two": 2, "three": 20}  # step 1 skipped
    assert (app_dir() / "sub" / "thing.before-1.json").is_file()


def test_bad_registry_is_refused(monkeypatch):
    monkeypatch.setitem(migrate.MIGRATIONS, "x.json", [(2, lambda d: d)])
    with pytest.raises(ValueError):
        migrate.run()


def test_stamp():
    assert migrate.stamp("queue.json", {"items": []}) == {"items": [], "_v": 1}
    assert migrate.stamp("not-registered.json", {"a": 1}) == {"a": 1}


# ------------------------------------------------- today's readers, after the migration

def test_readers_still_read_the_migrated_files(old_files):
    migrate.run()

    s = service.load_settings()
    assert s["out_dir"] == "D:\\Bajadas" and s["musicbrainz"] is True and "_v" not in s

    m = mediadl.load_settings()
    ids = [p["id"] for p in m["presets"]]
    assert "mine" in ids and m["default_preset"] == "mine" and m["daily_limit"] == 50
    stable = _get("media.json")["presets"][1]["id"]
    assert stable in ids and stable in [p["id"] for p in mediadl.load_settings()["presets"]]

    svc = service.Service(lambda e: None)
    assert [i["kind"] for i in svc._queue] == ["media"]
    assert set(svc._holds) == {"media"} and svc._holds["media"]["until"] == 5e9
    assert [f["id"] for f in svc.follows_list()["follows"]] == ["b2"]


def test_readers_still_read_unmigrated_old_files(old_files):
    """The readers' own compatibility code stays: an old file not yet migrated still works."""
    svc = service.Service(lambda e: None)
    assert set(svc._holds) == {"media"}
    assert service.load_settings()["wallhaven_user"] == "someone"


@pytest.mark.parametrize("mark", ["ui-settings.json", "media.json", "follows.json"])
def test_first_run_only_on_an_unused_bakin(mark):
    # PLAN 0.22.2: the guided start shows only when nothing says Bakin was used here
    _put(mark, {"_v": 1})
    assert service.Service(lambda e: None).get_state()["first_run"] is False


def test_first_run_then_done():
    svc = service.Service(lambda e: None)
    assert svc.get_state()["first_run"] is True
    assert svc.first_run_done()["ok"] and svc.get_state()["first_run"] is False
    assert (app_dir() / "ui-settings.json").is_file()
    assert service.Service(lambda e: None).get_state()["first_run"] is False
