"""Cookies that stop logging in: noticed from the file and from downloads,
reported without ever reading the values into anything kept or shown."""

import json
import os
import threading
import time

import pytest

from bakin import mediadl, service, tools

SECRET = "zzSECRETzz"


def cookies(path, *, expires, session=True):
    rows = ["# Netscape HTTP Cookie File", ""]
    rows.append(f".youtube.com\tTRUE\t/\tTRUE\t{expires}\tPREF\t{SECRET}")
    if session:
        rows.append(f"#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t{expires}\t__Secure-3PSID\t{SECRET}")
        rows.append(f".google.com\tTRUE\t/\tFALSE\t{expires + 999}\tSAPISID\t{SECRET}")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def preset(**kw):
    return mediadl.clean_preset(mediadl.new_preset("P", **kw))


def test_file_status_reads_names_and_expiry_never_values(tmp_path):
    soon = int(time.time()) + 86400
    st = mediadl.cookies_file_status(str(cookies(tmp_path / "c.txt", expires=soon)))
    assert st["exists"] and st["youtube_session"] and st["count"] == 3
    assert st["expires"] == soon and not st["expired"]
    assert SECRET not in json.dumps(st)
    assert mediadl.cookies_file_status(str(cookies(tmp_path / "old.txt", expires=1000)))["expired"]
    assert not mediadl.cookies_file_status(str(cookies(tmp_path / "anon.txt", expires=soon, session=False)))["youtube_session"]
    assert not mediadl.cookies_file_status(str(tmp_path / "missing.txt"))["exists"]


def _download(monkeypatch, tmp_path, with_cookies_result):
    def fake_run(self, args):
        if "--cookies" in args:
            return with_cookies_result
        return 1, {"done": [], "errors": ["[youtube] x: Sign in to confirm your age. This video may be inappropriate"],
                   "started": ["x"], "n_entries": None}
    monkeypatch.setattr(mediadl.Runner, "run", fake_run)
    monkeypatch.setattr(tools, "find", lambda *a, **k: None)
    f = cookies(tmp_path / "c.txt", expires=int(time.time()) + 86400)
    return mediadl.download(["https://youtu.be/x"], preset(cookies_mode="auto", cookies_source="file", cookies_file=str(f)),
                            tmp_path, lambda e: None, threading.Event())


def test_cookies_that_still_get_the_age_gate_are_bad(tmp_path, monkeypatch):
    stats = _download(monkeypatch, tmp_path, (1, {"done": [], "started": ["x"], "n_entries": None,
                                                "errors": ["[youtube] x: Sign in to confirm your age"]}))
    assert stats.cookies_bad


def test_rotated_cookies_warning_is_bad(tmp_path, monkeypatch):
    stats = _download(monkeypatch, tmp_path, (1, {"done": [], "started": ["x"], "n_entries": None, "errors": [],
                                                "cookies_expired": True}))
    assert stats.cookies_bad


def test_working_cookies_are_fine(tmp_path, monkeypatch):
    stats = _download(monkeypatch, tmp_path, (0, {"done": [{"id": "x", "filepath": ""}], "errors": [],
                                                "started": ["x"], "n_entries": None}))
    assert stats.done == 1 and not stats.cookies_bad


def test_runner_notices_the_rotation_warning():
    seen = {"done": [], "errors": [], "started": [], "n_entries": None, "cookies_expired": False}
    r = mediadl.Runner(lambda e: None, threading.Event())
    r._line("WARNING: [youtube] The provided YouTube account cookies are no longer valid. They have likely been "
            "rotated in the browser as a security measure.", seen)
    assert seen["cookies_expired"]


def test_service_reports_once_and_a_new_export_clears_it(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "missing", lambda required=None: [])
    monkeypatch.setattr(service.Service, "_ensure_worker", lambda self: None)
    service.save_settings({**service.load_settings(), "out_dir": str(tmp_path / "dl")})
    f = cookies(tmp_path / "c.txt", expires=int(time.time()) + 86400)
    s = mediadl.load_settings()
    for p in s["presets"]:
        if p["cookies_mode"] != "off":
            p.update(cookies_source="file", cookies_file=str(f))
    mediadl.save_settings(s)
    old = time.time() - 3600
    os.utime(f, (old, old))

    def fake_download(urls, preset, root, emit, cancel, **kw):
        st = mediadl.MediaStats(urls=1, failed=1)
        st.cookies_bad = True
        return st
    monkeypatch.setattr(mediadl, "download", fake_download)
    events = []
    a = service.Service(events.append)
    for v in ("aaaaaaaaaaa", "bbbbbbbbbbb"):
        a.start("download", f"https://www.youtube.com/watch?v={v}", {"preset": "bimusic"})
        a._run_media(a._queue.pop(0))
    assert [e["kind"] for e in events].count("cookies_bad") == 1
    st = a.cookies_status()["cookies"]
    assert len(st) == 1 and st[0]["bad"] and st[0]["youtube_session"]
    assert SECRET not in json.dumps(st) and SECRET not in a._cookies_health_file().read_text(encoding="utf-8")
    cookies(f, expires=int(time.time()) + 86400)        # the user exports a new one
    assert not a.cookies_status()["cookies"][0]["bad"]


def test_new_cookies_that_fail_again_are_reported_again(monkeypatch, tmp_path):
    monkeypatch.setattr(service.Service, "_ensure_worker", lambda self: None)
    f = cookies(tmp_path / "c.txt", expires=int(time.time()) + 86400)
    events = []
    a = service.Service(events.append)
    p = preset(cookies_mode="auto", cookies_source="file", cookies_file=str(f))
    old = time.time() - 3600
    os.utime(f, (old, old))
    a._mark_cookies_bad(p)
    a._mark_cookies_bad(p)
    assert len(events) == 1
    cookies(f, expires=int(time.time()) + 86400)        # re-exported, then those fail too
    a._mark_cookies_bad(p)
    assert len(events) == 2
