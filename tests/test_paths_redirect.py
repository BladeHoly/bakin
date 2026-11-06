"""PLAN 0.18.4: the data folder moved elsewhere, and back when it isn't there."""

from bakin import paths, service, tools


def test_redirect(monkeypatch, tmp_path):
    base = tmp_path / "BakinData"
    base.mkdir()
    moved = tmp_path / "otro disco" / "BakinData"
    moved.mkdir(parents=True)
    (base / paths.REDIRECT).write_text(str(moved), encoding="utf-8")
    assert paths.redirect_target(base) == moved
    moved.rmdir()  # the disk is gone
    assert paths.redirect_target(base) is None
    (base / paths.REDIRECT).write_text("relativa", encoding="utf-8")
    assert paths.redirect_target(base) is None


def test_moving_the_data_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "missing", lambda required=None: [])
    monkeypatch.setattr(service.Service, "_ensure_worker", lambda self: None)
    default = tmp_path / "local" / "BakinData"
    default.mkdir(parents=True)
    monkeypatch.setattr(paths, "default_dir", lambda: default)
    s = service.Service()
    (paths.app_dir() / "music").mkdir(exist_ok=True)
    (paths.app_dir() / "music" / "plays.json").write_text("{}", encoding="utf-8")
    (paths.app_dir() / "vault").mkdir(exist_ok=True)
    (paths.app_dir() / "vault" / "run-abc.txt").write_text("plain copy", encoding="utf-8")
    dest = tmp_path / "D" / "Bakin"
    r = s.data_dir_move(str(dest))
    assert r["ok"] and (dest / "music" / "plays.json").is_file()
    assert not (dest / "vault" / "run-abc.txt").exists()  # plain cookie copies never travel
    assert (default / paths.REDIRECT).read_text(encoding="utf-8") == str(dest)
    again = s.data_dir_move(str(dest))  # not empty: into a BakinData folder inside it
    assert again["ok"] and again["path"] == str(dest / "BakinData")
    assert not s.data_dir_move(str(dest))["ok"]  # that one is full too: refused
    assert not s.data_dir_move("relativa")["ok"]
