"""Moving Bakin's download folders to a new download folder, and undoing it."""

import errno
import os
import threading

import pytest

from bakin import relocate, service


def make_account(root, name, photos=3):
    d = root / name
    (d / "2026" / "01").mkdir(parents=True)
    (d / relocate.MEDIA_MARKER).write_text('{"preset": "Wallhaven"}', encoding="utf-8")
    for i in range(photos):
        (d / "2026" / "01" / f"p{i}.jpg").write_bytes(os.urandom(5000 + i))
    return d


def snapshot(d):
    return {p.relative_to(d).as_posix(): p.read_bytes() for p in d.rglob("*") if p.is_file()}


@pytest.fixture
def cross_drive(monkeypatch):
    """Make folder renames fail like they do between two drives."""
    real = os.rename

    def rename(a, b):
        if os.path.isdir(a) and not str(a).endswith(relocate.STAGING):
            raise OSError(errno.EXDEV, "cross-device link")
        return real(a, b)
    monkeypatch.setattr(relocate.os, "rename", rename)


@pytest.mark.parametrize("drives", ["same", "cross"])
def test_moves_only_account_folders_byte_identical(tmp_path, request, drives):
    if drives == "cross":
        request.getfixturevalue("cross_drive")
    old, new = tmp_path / "old", tmp_path / "new"
    a = make_account(old, "alice")
    before = snapshot(a)
    (old / "vacation.jpg").write_bytes(b"user's own file")   # not Bakin's: stays
    (old / "notes").mkdir()
    res = relocate.move_accounts(old, new)
    assert res.moved == ["alice"] and not res.skipped
    assert snapshot(new / "alice") == before
    assert not a.exists()
    assert (old / "vacation.jpg").read_bytes() == b"user's own file" and (old / "notes").is_dir()
    assert not list(new.glob("*" + relocate.STAGING))


def test_existing_folder_at_destination_is_not_overwritten(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    make_account(old, "alice")
    (new / "alice").mkdir(parents=True)
    (new / "alice" / "keep.txt").write_text("x")
    res = relocate.move_accounts(old, new)
    assert res.moved == [] and res.skipped[0]["folder"] == "alice"
    assert (old / "alice" / relocate.MEDIA_MARKER).is_file() and (new / "alice" / "keep.txt").is_file()


def test_failed_copy_keeps_the_original(tmp_path, cross_drive, monkeypatch):
    old, new = tmp_path / "old", tmp_path / "new"
    a = make_account(old, "alice")
    before = snapshot(a)
    monkeypatch.setattr(relocate, "_sha256", lambda p: "0" * 64)   # every read-back "differs"
    res = relocate.move_accounts(old, new)
    assert res.moved == [] and "no coincide" in res.skipped[0]["reason"]
    assert snapshot(a) == before
    assert not (new / "alice").exists() and not list(new.glob("*" + relocate.STAGING))


def test_target_inside_an_account_is_refused(tmp_path):
    old = tmp_path / "old"
    make_account(old, "alice")
    assert relocate.check_target(old, old / "alice" / "x")
    assert relocate.check_target(old, old) == "Es la misma carpeta."
    assert relocate.check_target(old, old / "Bakin") is None   # a subfolder of the old one is fine


# ------------------------------------------------------------- the service

def run_move(svc, call):
    done = threading.Event()
    events = []

    def push(ev):
        events.append(ev)
        if ev["kind"] == "move_done":
            done.set()
    svc._push_fn = push
    res = call()
    assert res["ok"] and res.get("moving"), res
    assert done.wait(10)
    return events[-1]


def test_change_folder_asks_then_moves_then_undoes(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    make_account(old, "alice")
    before = snapshot(old / "alice")
    service.save_settings({**service.load_settings(), "out_dir": str(old)})
    svc = service.Service()

    ask = svc.set_out_dir(str(new))
    assert ask["ask"] and ask["profiles"] == ["alice"] and ask["files"] == 4
    assert service.load_settings()["out_dir"] == str(old)       # nothing changed yet

    done = run_move(svc, lambda: svc.set_out_dir(str(new), move=True))
    assert done["ok"] and done["moved"] == ["alice"]
    assert service.load_settings()["out_dir"] == str(new.resolve())
    assert done["last_move"] == {"from": str(old.resolve()), "to": str(new.resolve()), "profiles": 1}
    assert snapshot(new / "alice") == before

    back = run_move(svc, svc.undo_move)
    assert back["ok"] and back["last_move"] is None
    assert service.load_settings()["out_dir"] == str(old.resolve())
    assert snapshot(old / "alice") == before and not (new / "alice").exists()


def test_switch_without_moving_can_be_undone(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    make_account(old, "alice")
    service.save_settings({**service.load_settings(), "out_dir": str(old)})
    svc = service.Service()
    r = svc.set_out_dir(str(new), move=False)
    assert r["ok"] and r["last_move"]["profiles"] == 0
    assert (old / "alice").is_dir()
    assert svc.undo_move()["ok"]
    assert service.load_settings()["out_dir"] == str(old.resolve())
    assert svc.get_state()["last_move"] is None


def test_empty_old_folder_switches_without_asking(tmp_path):
    service.save_settings({**service.load_settings(), "out_dir": str(tmp_path / "empty")})
    r = service.Service().set_out_dir(str(tmp_path / "new"))
    assert r["ok"] and r["changed"]


def test_relative_or_private_paths_refused(tmp_path):
    svc = service.Service()
    assert not svc.set_out_dir("descargas")["ok"]
    from bakin.paths import app_dir
    assert not svc.set_out_dir(str(app_dir() / "x"), move=False)["ok"]


def test_move_refused_while_jobs_are_queued(tmp_path):
    old = tmp_path / "old"
    make_account(old, "alice")
    service.save_settings({**service.load_settings(), "out_dir": str(old)})
    svc = service.Service()
    svc._queue.append({"id": 1})
    r = svc.set_out_dir(str(tmp_path / "new"), move=True)
    assert not r["ok"] and "Espera" in r["error"]
