"""Follows (channels, playlists) and download hours."""

import datetime as dt
import time

import pytest

from bakin import mediadl, service, tools


@pytest.fixture
def svc(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "missing", lambda required=None: [])
    monkeypatch.setattr(service.Service, "_ensure_worker", lambda self: None)
    service.save_settings({**service.load_settings(), "out_dir": str(tmp_path / "dl")})
    music = tmp_path / "Music"
    s = mediadl.load_settings()
    for p in s["presets"]:
        if p["mode"] == "audio":
            p["folder"] = str(music)
    mediadl.save_settings(s)
    return service.Service(), music


def set_hours(a, b):
    s = mediadl.load_settings()
    mediadl.save_settings({**s, "hours": {"enabled": True, "from": a, "to": b}})


@pytest.mark.parametrize("a,b,hour,inside", [
    (1, 8, 3, True), (1, 8, 8, False), (1, 8, 23, False), (22, 6, 23, True), (22, 6, 2, True), (22, 6, 12, False),
])
def test_download_hours(svc, a, b, hour, inside):
    s, _ = svc
    set_hours(a, b)
    now = dt.datetime(2026, 11, 1, hour, 30)
    out = s._outside_hours(now)
    assert (out is None) == inside
    if out:
        start = dt.datetime.fromtimestamp(out[1])
        assert start.hour == a and start.minute == 0 and start > now


def test_worker_waits_outside_hours(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "missing", lambda required=None: [])
    service.save_settings({**service.load_settings(), "out_dir": str(tmp_path / "dl")})
    hour = dt.datetime.now().hour
    set_hours((hour + 2) % 24, (hour + 3) % 24)            # a window that isn't now
    ran = []
    monkeypatch.setattr(service.Service, "_run_media", lambda self, item: ran.append(item) or "ok")
    a = service.Service()
    a.start("download", "https://www.youtube.com/watch?v=aaaaaaaaaaa", {"preset": "bimusic"})
    a._worker.join(5)
    assert ran == [] and a._paused and "Fuera del horario" in a._pause_reason
    assert a.set_hours(False)["ok"] and not a._paused     # turning the hours off resumes


def test_follow_only_new_marks_existing_as_seen(svc, monkeypatch):
    s, music = svc
    monkeypatch.setattr(mediadl, "list_entries", lambda url, preset: [("youtube", "aaaaaaaaaaa"), ("youtube", "bbbbbbbbbbb")])
    r = s.follow_add("https://www.youtube.com/@canal", "bimusic", 12, only_new=True)
    assert r["ok"] and r["follow"]["marked_seen"] == 2 and r["follow"]["every_hours"] == 12
    assert (music / "archive.txt").read_text().splitlines() == ["youtube aaaaaaaaaaa", "youtube bbbbbbbbbbb"]
    assert not s.follow_add("https://www.youtube.com/@canal/", "bimusic")["ok"]          # already followed
    assert s._check_follows(time.time()) == 0                                             # just checked
    assert s._check_follows(time.time() + 13 * 3600) == 1                                 # due: queued
    assert s._queue[-1]["url"] == "https://www.youtube.com/@canal" and s._queue[-1]["preset"]["id"] == "bimusic"
    again = s.follows_list()["follows"][0]
    assert again["last_check"] > time.time() - 60
