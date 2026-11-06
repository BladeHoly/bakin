"""Application service (bakin/service.py) and the desktop page it serves, without a window."""

import base64
import hashlib
import json
import re
import threading
from pathlib import Path

import pytest

from bakin import service

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "desktop" / "ui"
TAURI_CONF = ROOT / "desktop" / "src-tauri" / "tauri.conf.json"


# ------------------------------------------------------------------ the page

def test_csp_importmap_hash_matches_inline_import_map():
    html = (UI / "index.html").read_text(encoding="utf-8")
    importmap = re.search(r'<script type="importmap">(.*?)</script>', html, re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(importmap.encode()).digest()).decode()
    csp = json.loads(TAURI_CONF.read_text(encoding="utf-8"))["app"]["security"]["csp"]
    script_src = csp["script-src"] if isinstance(csp, dict) else csp.split("script-src")[1].split(";")[0]
    assert f"'sha256-{digest}'" in script_src
    assert "unsafe-inline" not in script_src


def test_vendored_library_is_complete_and_local():
    vendor = UI / "vendor" / "glass-midas"
    assert (vendor / "core" / "index.js").is_file()
    assert (vendor / "components" / "index.js").is_file()
    assert (vendor / "components" / "styles.css").is_file()
    for js in vendor.rglob("*.js"):
        text = js.read_text(encoding="utf-8")
        assert "https://" not in text and "fetch(" not in text, js


def test_page_only_calls_whitelisted_engine_methods():
    js = (UI / "app.js").read_text(encoding="utf-8")
    rust = (ROOT / "desktop" / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")
    allowed = set(re.findall(r'"(\w+)"', rust.split("ENGINE_METHODS")[1].split("];")[0]))
    from bakin.sidecar import METHODS
    used = set(re.findall(r"\bcall\('(\w+)'", js))
    assert used and used <= allowed <= set(METHODS)


# ------------------------------------------------------------------ settings

def test_default_folder_is_the_users_downloads():
    from bakin.paths import user_downloads_dir
    assert service.DEFAULT_SETTINGS["out_dir"] == str(user_downloads_dir())
    assert service.load_settings()["out_dir"] == str(user_downloads_dir())  # nothing saved yet


# ------------------------------------------------------------------ links

def test_start_rejects_bad_urls_without_network():
    api = service.Service()
    r = api.start("download", "esto no es un enlace", {})
    assert not r["ok"] and r["field"] == "url"
    r = api.start("download", "javascript:alert(1)", {})
    assert not r["ok"] and r["field"] == "url"
    assert not api.start("bogus", "https://wallhaven.cc/w/aaaa1", {})["ok"]


# ------------------------------------------------------------------- files

def test_make_thumb_does_not_modify_file(tmp_path):
    from conftest import jpeg_bytes
    p = tmp_path / "x.jpg"
    data = jpeg_bytes(1200, 1600)
    p.write_bytes(data)
    uri = service.make_thumb(p)
    assert uri.startswith("data:image/jpeg;base64,") and p.read_bytes() == data


# ------------------------------------------------------------------- queue

class Runner:
    """Replaces Service._run_wallhaven: each job blocks until released, then reports `outcome`."""

    def __init__(self, outcomes=None):
        self.outcomes = dict(outcomes or {})
        self.started = []
        self.gates: dict[str, threading.Event] = {}
        self.began = threading.Condition()

    def __call__(self, svc, item):
        name = item["job"]["id"]
        gate = self.gates.setdefault(name, threading.Event())
        with self.began:
            self.started.append(name)
            self.began.notify_all()
        gate.wait(5)
        return self.outcomes.get(name, "ok")

    def wait_started(self, n):
        with self.began:
            assert self.began.wait_for(lambda: len(self.started) >= n, 5)

    def release(self, name):
        self.gates.setdefault(name, threading.Event()).set()


@pytest.fixture
def queued(monkeypatch, tmp_path):
    events = []
    runner = Runner()
    monkeypatch.setattr(service.Service, "_run_wallhaven", lambda self, item: runner(self, item))
    svc = service.Service(events.append)
    svc.opts = {"out_dir": str(tmp_path / "dl")}
    return svc, runner, events


def url(name):
    return f"https://wallhaven.cc/w/{name}"


A, B, C = "aaaa1", "bbbb2", "cccc3"


def finish(svc):
    if svc._worker:
        svc._worker.join(5)
        assert not svc._worker.is_alive()


def test_jobs_queue_and_run_in_order(queued):
    svc, runner, events = queued
    assert svc.start("download", url(A), svc.opts)["waiting"] == 0
    runner.wait_started(1)
    assert svc.start("download", url(B), svc.opts)["waiting"] == 1
    assert svc.start("download", url(C), svc.opts)["waiting"] == 2
    q = svc.get_state()["queue"]["items"]
    assert [i["status"] for i in q] == ["running", "pending", "pending"]
    for name in (A, B, C):
        runner.release(name)
    finish(svc)
    assert runner.started == [A, B, C]
    assert events[-1]["kind"] == "queue" and events[-1]["items"] == [] and events[-1]["paused"] is False


def test_same_target_twice_is_refused(queued):
    svc, runner, _ = queued
    svc.start("download", url(A), svc.opts)
    runner.wait_started(1)
    r = svc.start("download", url(A.upper()), svc.opts)
    assert not r["ok"] and "ya está en la cola" in r["error"]
    runner.release(A)
    finish(svc)


def test_remove_waiting_job(queued):
    svc, runner, _ = queued
    svc.start("download", url(A), svc.opts)
    runner.wait_started(1)
    svc.start("download", url(B), svc.opts)
    item = svc.get_state()["queue"]["items"][1]
    assert svc.remove_queued(item["id"]) == {"ok": True}
    runner.release(A)
    finish(svc)
    assert runner.started == [A]


def test_cancel_clears_the_queue(queued):
    svc, runner, _ = queued
    svc.start("download", url(A), svc.opts)
    runner.wait_started(1)
    svc.start("download", url(B), svc.opts)
    svc.cancel()
    assert svc._cancel.is_set()
    runner.release(A)
    finish(svc)
    assert runner.started == [A]


def test_a_stop_pauses_the_rest_until_resumed(queued):
    svc, runner, _ = queued
    runner.outcomes[A] = "stopped"   # e.g. HTTP 429
    svc.start("download", url(A), svc.opts)
    runner.wait_started(1)
    svc.start("download", url(B), svc.opts)
    runner.release(A)
    finish(svc)
    state = svc.get_state()["queue"]
    assert runner.started == [A] and state["paused"]
    assert [i["status"] for i in state["items"]] == ["paused"]
    # Adding more while paused just waits too.
    assert svc.start("download", url(C), svc.opts)["waiting"] == 2
    assert not svc.job_running()
    runner.release(B)
    runner.release(C)
    svc.resume_queue()
    finish(svc)
    assert runner.started == [A, B, C]


# --------------------------------------------------------------- versions

def test_versions_agree_and_changelog_is_current():
    import subprocess
    import sys
    import tomllib
    from bakin import __version__
    conf = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    pkg = json.loads((ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))
    cargo = tomllib.loads((ROOT / "desktop" / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8"))
    py = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    versions = {conf["version"], pkg["version"], cargo["package"]["version"], py["project"]["version"], __version__}
    assert len(versions) == 1, versions
    log = json.loads((UI / "changelog.json").read_text(encoding="utf-8"))
    assert log[0]["version"] == conf["version"] and all(r["items"] for r in log)
    r = subprocess.run([sys.executable, str(ROOT / "packaging" / "changelog.py"), "--check"])
    assert r.returncode == 0


def test_updater_only_trusts_signed_releases_from_the_public_repo():
    conf = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    up = conf["plugins"]["updater"]
    assert up["pubkey"] and conf["bundle"]["createUpdaterArtifacts"] is True
    assert up["endpoints"] == ["https://github.com/BladeHoly/bakin/releases/latest/download/latest.json"]
    assert "dangerousInsecureTransportProtocol" not in up


def test_data_moves_out_of_the_default_install_folder(tmp_path, monkeypatch):
    from bakin import paths
    monkeypatch.delenv("BAKIN_HOME", raising=False)
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    old = tmp_path / "bakin"                       # also the installer's default folder
    (old / "tools" / "yt-dlp").mkdir(parents=True)
    (old / "tools" / "yt-dlp" / "yt-dlp.exe").write_bytes(b"x")
    (old / "ui-settings.json").write_text("{}")
    (old / "bakin-desktop.exe").write_bytes(b"app")  # the installed app: left alone
    new = paths.app_dir()
    assert new == tmp_path / "BakinData"
    assert (new / "tools" / "yt-dlp" / "yt-dlp.exe").is_file() and (new / "ui-settings.json").is_file()
    assert (old / "bakin-desktop.exe").is_file() and not (old / "tools").exists()


def test_media_links_queue_with_preset_and_format_override(monkeypatch, tmp_path):
    from bakin import mediadl, tools
    monkeypatch.setattr(tools, "missing", lambda required=None: [])
    monkeypatch.setattr(service.Service, "_ensure_worker", lambda self: None)   # don't run yt-dlp
    service.save_settings({**service.load_settings(), "out_dir": str(tmp_path)})
    svc = service.Service()
    ms = mediadl.load_settings()
    r = svc.start("download", "https://www.youtube.com/watch?v=jNQXAC9IVRw",
                  {"override": {"audio_format": "flac", "mode": "audio", "evil": "x"}})
    assert r["ok"] and r["media"]
    item = svc._queue[-1]
    assert item["preset"]["audio_format"] == "flac" and "evil" not in item["preset"]
    assert item["preset"]["id"] == ms["default_preset"] and "(FLAC)" in item["title"]
    # saved preset untouched
    assert mediadl.load_settings()["presets"][0]["audio_format"] == "opus"
    again = svc.start("download", "https://www.youtube.com/watch?v=jNQXAC9IVRw", {"override": {"audio_format": "flac"}})
    assert not again["ok"]
    other = svc.start("download", "https://www.youtube.com/watch?v=jNQXAC9IVRw", {"override": {"audio_format": "opus"}})
    assert other["ok"]


def test_media_needs_tools(monkeypatch, tmp_path):
    from bakin import tools
    monkeypatch.setattr(tools, "missing", lambda required=None: ["yt-dlp", "ffmpeg"])
    r = service.Service().start("download", "https://soundcloud.com/a/b", {})
    assert not r["ok"] and r["field"] == "tools" and r["missing"] == ["yt-dlp", "ffmpeg"]


def test_page_finds_selects_wrapped_by_gm_select():
    js = (UI / "app.js").read_text(encoding="utf-8")
    # gm-select moves a select's id to its button; the page's $ must undo that.
    assert "gm-select-trigger" in js.split("const $ =")[1].split("};")[0]


def test_convert_queue_validates_inputs(monkeypatch, tmp_path):
    from PIL import Image
    from bakin.paths import app_dir
    monkeypatch.setattr(service.Service, "_ensure_worker", lambda self: None)
    img = tmp_path / "a.png"
    Image.new("RGB", (8, 8)).save(img)
    secret = app_dir() / "ui-settings.json"
    secret.write_text("{}")
    svc = service.Service()
    insp = svc.convert_inspect([{"path": str(img)}, {"path": str(secret)}, {"path": "rel.png"}, {"id": "md:x|../a"}])
    assert [f["name"] for f in insp["files"]] == ["a.png"] and insp["skipped"] == 3
    assert insp["files"][0]["thumb"].startswith("data:image/jpeg")
    r = svc.convert_start([str(img), str(secret)], {"target": "webp"})
    assert r["ok"] and svc._queue[-1]["files"] == [img.resolve()] and svc._queue[-1]["title"].endswith("WEBP")
    song = tmp_path / "s.mp3"
    song.write_bytes(b"x")
    mixed = svc.convert_start([str(img), str(song)], {"target": "avif"})
    assert mixed["ok"] and mixed["skipped"] == 1 and svc._queue[-1]["files"] == [img.resolve()]
    assert not svc.convert_start([str(img)], {"target": "png"})["ok"]          # already PNG
    assert not svc.convert_start([str(img)], {"target": "webp", "out_dir": str(app_dir())})["ok"]
    assert not svc.convert_open(str(secret))


def test_the_preset_chosen_for_a_site_is_remembered_and_used(monkeypatch, tmp_path):
    from bakin import mediadl, tools
    monkeypatch.setattr(tools, "missing", lambda required=None: [])
    monkeypatch.setattr(service.Service, "_ensure_worker", lambda self: None)
    service.save_settings({**service.load_settings(), "out_dir": str(tmp_path / "dl")})
    svc = service.Service()
    video = next(p["id"] for p in mediadl.load_settings()["presets"] if p["mode"] == "video")
    assert svc.media_set_site_pref("https://www.youtube.com/watch?v=x", video) == {"ok": True, "site": "youtube.com", "preset": video}
    assert not svc.media_set_site_pref("https://www.youtube.com/watch?v=x", "no-such-preset")["ok"]
    assert mediadl.load_settings()["site_prefs"] == {"youtube.com": video}
    assert svc.start("download", "https://m.youtube.com/watch?v=aaaaaaaaaaa")["ok"]
    assert svc._queue[-1]["preset"]["id"] == video          # a direct download takes the site's preset
    assert svc.media_set_site_pref("https://youtube.com/", "")["ok"]
    assert mediadl.load_settings()["site_prefs"] == {}


def test_engine_sleeps_only_when_idle_and_says_when_to_wake(queued):
    svc, runner, _ = queued
    assert svc.sleep_if_idle(now=1000.0) == {"idle": True, "wake_at": None}
    # a running job keeps it awake
    svc.start("download", url(A), svc.opts)
    runner.wait_started(1)
    assert svc.sleep_if_idle()["idle"] is False
    runner.release(A)
    finish(svc)
    # a timed pause: wake when it ends
    svc._paused, svc._paused_until = True, 5000.0
    assert svc.sleep_if_idle(now=1000.0) == {"idle": True, "wake_at": 5000.0}
    # a follow due earlier wins; one due right now keeps it awake
    svc._save_follows([{"url": "https://example.com/c", "kind": "media", "enabled": True, "last_check": 0, "every_hours": 1}])
    assert svc.sleep_if_idle(now=1000.0) == {"idle": True, "wake_at": 3600.0}
    assert svc.sleep_if_idle(now=3600.0)["idle"] is False


def test_plays_counted_while_the_engine_slept_are_merged():
    from bakin import music
    p = music._file("plays-pending.jsonl")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"id": "s1", "at": 100}\n{"id": "s1", "at": 200}\nnot json\n{"id": "s2", "at": 50}\n', encoding="utf-8")
    music.merge_pending_plays()
    got = music.plays()
    assert got["s1"] == {"n": 2, "last": 200.0} and got["s2"]["n"] == 1
    assert not p.exists()
    assert json.loads(music._file("plays.json").read_text(encoding="utf-8"))["s1"]["n"] == 2


def test_waking_the_engine_lifts_a_pause_that_is_over(queued):
    # out of download hours, a daily limit or a YouTube pushback are timed pauses
    svc, _, _ = queued
    svc._paused, svc._paused_until, svc._pause_reason = True, 1.0, "Fuera del horario."
    svc._follows_checked = 10 ** 12  # resume_due also checks follows: now, not «in 5 min»
    assert svc.resume_due() == {"ok": True}
    assert not svc._paused and svc._follows_checked < 10 ** 12


def test_wallpaper_sources_list_each_collection_and_the_favourites(monkeypatch, tmp_path):
    from bakin import library
    out = tmp_path / "dl"
    (out / "Wallhaven").mkdir(parents=True)
    mine = tmp_path / "Mis fondos"
    mine.mkdir()
    monkeypatch.setattr(service, "load_settings", lambda: {"out_dir": str(out)})
    library.save_user_folders([mine])
    got = service.Service(lambda e: None).wallpaper_sources()["sources"]
    assert got[0]["kind"] == "favorites" and len(got[0]["metas"]) == len(got) - 1
    folders = {s["label"]: s["folders"] for s in got[1:]}
    assert folders["Mis fondos"] == [str(mine)]
    assert any(m["meta"].endswith(".json") for m in got[0]["metas"])


def test_wallpapers_get_past_a_music_library_held_by_the_daily_limit(queued):
    # 0.13 review: thousands of liked songs waiting for tomorrow kept Wallhaven out
    svc, runner, _ = queued
    with svc._lock:
        for i in range(2500):  # more than QUEUE_LIMIT
            svc._next_id += 1
            svc._queue.append({"id": svc._next_id, "kind": "media", "mode": "download", "title": f"tema {i}"})
    svc.pause_queue("Se llegó al límite diario.", until=9e9, scope="media")
    r = svc.start("download", url(A), svc.opts)
    assert r["ok"], r
    runner.wait_started(1)  # runs now, the music still waits
    assert runner.started == [A]
    state = svc.get_state()["queue"]
    assert state["paused"] and state["scope"] == "media"
    assert [i["status"] for i in state["items"][:2]] == ["running", "paused"]
    runner.release(A)
    finish(svc)
    assert len(svc._queue) == 2500 and all(q["kind"] == "media" for q in svc._queue)
    # not paused: a new one still goes ahead of the music
    svc.resume_queue()
    with svc._lock:
        svc._paused = True  # hold the worker to look at the order
        svc._pause_scope = "all"
        svc._start_wallhaven({"type": "wallpaper", "id": B}, {})
        assert svc._queue[0]["kind"] == "wallhaven" and svc._queue[1]["kind"] == "media"
        svc._queue.clear()
    # a pause by the user holds everything; one from Wallhaven only Wallhaven
    assert not svc._runnable({"kind": "wallhaven"})
    svc._pause_scope = "wallhaven"
    assert svc._runnable({"kind": "media"}) and not svc._runnable({"kind": "wallhaven"})
    svc.resume_queue()


def test_each_lane_pauses_and_resumes_on_its_own(queued):
    svc, runner, _ = queued
    svc.queue_pause("media")
    lanes = {x["id"]: x for x in svc.get_state()["queue"]["lanes"]}
    assert lanes["media"]["held"]["reason"] == "Pausada por ti." and lanes["wallhaven"]["held"] is None
    svc.start("download", url(A), svc.opts)  # a wallpaper: its lane is free
    runner.wait_started(1)
    runner.release(A)
    finish(svc)
    svc.queue_resume("media")
    assert not svc._holds
    # timed waits end each on its own
    svc.pause_queue("límite", until=100.0, scope="media")
    svc.pause_queue("429", until=200.0, scope="wallhaven")
    svc._tick(now=150.0)
    assert set(svc._holds) == {"wallhaven"}
    svc._tick(now=250.0)
    assert not svc._holds
    # the download hours hold downloads, not conversions (local)
    svc.pause_queue("Fuera del horario", until=9e9, scope="downloads")
    assert svc._runnable({"kind": "convert"}) and not svc._runnable({"kind": "wallhaven"}) and not svc._runnable({"kind": "media"})
    svc.queue_resume()


def test_lane_pauses_are_saved_and_the_old_format_still_loads(tmp_path):
    import json as _json
    svc = service.Service(lambda e: None)
    svc.pause_queue("Se llegó al límite diario.", until=9e9, scope="media")
    svc._save_queue(force=True)
    saved = _json.loads(svc._qfile.read_text(encoding="utf-8"))
    assert set(saved["holds"]) == {"media"}
    again = service.Service(lambda e: None)
    assert set(again._holds) == {"media"} and again._holds["media"]["until"] == 9e9
    # a queue.json written by 0.13 and before (the user's, with ~4000 liked songs waiting)
    svc._qfile.write_text(_json.dumps({"items": [], "paused": True, "paused_until": 5e9, "reason": "límite", "scope": "media"}), encoding="utf-8")
    old = service.Service(lambda e: None)
    assert set(old._holds) == {"media"} and old._paused_until == 5e9
    svc._qfile.write_text(_json.dumps({"items": [], "paused": True, "paused_until": None, "reason": "por ti"}), encoding="utf-8")
    assert set(service.Service(lambda e: None)._holds) == set(service.LANES)


def test_yt_dlp_is_checked_once_a_day_and_right_after_youtube_pushes_back(monkeypatch):
    # updating yt-dlp is what fixed YouTube every time (the PO Token provider was never needed)
    import time
    from bakin import mediadl, tools
    svc = service.Service(lambda e: None)
    checks = []
    # only this service's checks: other tests' services still tick in the background
    monkeypatch.setattr(service.Service, "media_maintenance", lambda self, force=False: (checks.append(1) if self is svc else None) or {"ok": True})
    st = tools._state()
    st["yt-dlp"] = {"checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    tools._save_state(st)
    svc._tick(now=time.time() + 60)
    assert checks == []  # checked today (on disk: the engine sleeps and wakes often)
    svc._tick(now=time.time() + 25 * 3600)
    assert checks == [1]
    svc._ytdlp_force = True  # YouTube stopped a download
    svc._tick(now=time.time() + 25 * 3600 + 30)
    assert checks == [1, 1]
    # and the PO Token option is gone
    assert "pot_provider" not in mediadl.load_settings()
    assert not svc.media_set_option("pot_provider", True)["ok"]
