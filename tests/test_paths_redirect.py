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


def test_app_dir_without_bakin_home_follows_the_redirect(monkeypatch, tmp_path):
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.delenv("BAKIN_HOME")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    base = tmp_path / "local" / paths.WINDOWS_DATA_NAME
    assert paths.app_dir() == base and base.is_dir()
    moved = tmp_path / "E" / "Bakin"
    moved.mkdir(parents=True)
    (base / paths.REDIRECT).write_text(str(moved), encoding="utf-8")
    assert paths.app_dir() == moved and paths.redirect_missing() == ""
    moved.rmdir()  # the disk is unplugged: the usual folder, and Bakin says where it was
    assert paths.app_dir() == base and paths.redirect_missing() == str(moved)
    (base / paths.REDIRECT).unlink()
    assert paths.redirect_missing() == ""


def test_redirect_missing_only_on_a_real_install():
    assert paths.redirect_missing() == ""  # BAKIN_HOME (tests, the test copy)


def test_data_moves_out_of_the_install_folder(monkeypatch, tmp_path):
    old = tmp_path / paths.APP_NAME  # %LOCALAPPDATA%\bakin, before 0.4
    (old / "logs").mkdir(parents=True)
    (old / "logs" / "bakin.log").write_text("x", encoding="utf-8")
    (old / "media.json").write_text("{}", encoding="utf-8")
    (old / "Bakin.exe").write_text("the app", encoding="utf-8")  # the installed app's: stays
    new = tmp_path / paths.WINDOWS_DATA_NAME
    paths._move_out_of_install_folder(new)
    assert (new / "logs" / "bakin.log").is_file() and (new / "media.json").is_file()
    assert (old / "Bakin.exe").is_file() and not (new / "Bakin.exe").exists()
    paths._move_out_of_install_folder(tmp_path / "nothing" / "BakinData")  # no old folder: nothing to do
    assert not (tmp_path / "nothing").exists()


def test_settings_come_over_from_igmax(tmp_path):
    old = tmp_path / paths.LEGACY_APP_NAME
    old.mkdir()
    (old / "config.toml").write_text("a = 1", encoding="utf-8")
    (old / "cache.db").write_text("x", encoding="utf-8")
    new = tmp_path / paths.APP_NAME
    paths._copy_legacy_settings(new)
    assert (new / "config.toml").read_text(encoding="utf-8") == "a = 1" and not (new / "cache.db").exists()
    paths._copy_legacy_settings(tmp_path / "x" / "bakin")  # no igmax folder
    assert not (tmp_path / "x").exists()


def test_is_inside(tmp_path):
    assert paths.is_inside(tmp_path / "a" / "b", tmp_path)
    assert not paths.is_inside(tmp_path, tmp_path / "a")
