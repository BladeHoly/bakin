"""Background queue: saved to disk, daily limit, timed pauses, private lists."""

import json
import threading
import time

import pytest

from bakin import mediadl, service, tools


@pytest.fixture
def svc(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "missing", lambda required=None: [])
    service.save_settings({**service.load_settings(), "out_dir": str(tmp_path / "dl")})
    return service.Service


def no_worker(monkeypatch):
    monkeypatch.setattr(service.Service, "_ensure_worker", lambda self: None)


def test_queue_survives_a_restart(svc, monkeypatch):
    no_worker(monkeypatch)
    a = svc()
    for v in ("aaaaaaaaaaa", "bbbbbbbbbbb"):
        assert a.start("download", f"https://www.youtube.com/watch?v={v}", {"preset": "bimusic"})["ok"]
    a.pause_queue("prueba", until=time.time() + 3600)
    a._save_queue(force=True)
    b = svc()
    assert [i["url"][-11:] for i in b._queue] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    assert b._paused and b._paused_until and b._pause_reason == "prueba"
    assert b._queue[0]["preset"]["id"] == "bimusic"


def test_shutdown_keeps_the_running_job_first(svc, monkeypatch):
    no_worker(monkeypatch)
    a = svc()
    a.start("download", "https://www.youtube.com/watch?v=aaaaaaaaaaa", {"preset": "bimusic"})
    a.start("download", "https://www.youtube.com/watch?v=bbbbbbbbbbb", {"preset": "bimusic"})
    a._current = a._queue.pop(0)                     # as if it were downloading
    a.shutdown()
    assert a._cancel.is_set()
    b = svc()
    assert [i["url"][-11:] for i in b._queue] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]


def test_timed_pause_lifts_itself(svc, monkeypatch):
    no_worker(monkeypatch)
    a = svc()
    a.pause_queue("límite", until=time.time() + 60)
    assert not a._tick()
    assert a._tick(now=time.time() + 61)
    assert not a._paused and a._paused_until is None


def test_daily_limit_pauses_until_tomorrow(svc, monkeypatch):
    s = mediadl.load_settings()
    mediadl.save_settings({**s, "daily_limit": 2, "downloads_today": {"date": time.strftime("%Y-%m-%d"), "count": 2}})
    ran = []
    monkeypatch.setattr(service.Service, "_run_media", lambda self, item: ran.append(item) or "ok")
    a = svc()
    a.start("download", "https://www.youtube.com/watch?v=aaaaaaaaaaa", {"preset": "bimusic"})
    a._worker.join(5)
    assert ran == [] and a._paused and a._paused_until > time.time() + 60
    assert "límite diario" in a._pause_reason and len(a._queue) == 1


def test_media_job_gets_the_remaining_budget_and_counts(svc, monkeypatch, tmp_path):
    s = mediadl.load_settings()
    mediadl.save_settings({**s, "daily_limit": 10, "downloads_today": {"date": time.strftime("%Y-%m-%d"), "count": 7}})
    seen = {}

    def fake_download(urls, preset, root, emit, cancel, **kw):
        seen.update(kw)
        st = mediadl.MediaStats(urls=1, done=3)
        st.limit_reached = True
        return st
    monkeypatch.setattr(mediadl, "download", fake_download)
    no_worker(monkeypatch)
    a = svc()
    a.start("download", "https://www.youtube.com/playlist?list=PLx", {"preset": "bimusic"})
    item = a._queue.pop(0)
    assert a._run_media(item) == "requeue"
    assert seen["max_downloads"] == 3
    assert mediadl.load_settings()["downloads_today"]["count"] == 10


def test_youtube_pushback_pauses_for_hours_and_retries_that_link_first(svc, monkeypatch):
    def fake_download(urls, preset, root, emit, cancel, **kw):
        st = mediadl.MediaStats(urls=1)
        st.blocked = True
        return st
    monkeypatch.setattr(mediadl, "download", fake_download)
    no_worker(monkeypatch)
    a = svc()
    a.start("download", "https://www.youtube.com/watch?v=aaaaaaaaaaa", {"preset": "bimusic"})
    a.start("download", "https://www.youtube.com/watch?v=bbbbbbbbbbb", {"preset": "bimusic"})
    item = a._queue.pop(0)
    a._run_media(item)
    assert a._paused and a._paused_until > time.time() + 5 * 3600
    assert [i["url"][-11:] for i in a._queue] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]


def test_liked_music_is_listed_once_then_downloaded_without_the_account(svc, monkeypatch, tmp_path):
    no_worker(monkeypatch)
    music = tmp_path / "Music"
    music.mkdir()
    (music / "archive.txt").write_text("youtube bbbbbbbbbbb\n", encoding="utf-8")
    s = mediadl.load_settings()
    for p in s["presets"]:
        if p["mode"] == "audio":
            p["folder"] = str(music)
    mediadl.save_settings(s)
    listed = {}
    monkeypatch.setattr(mediadl, "list_ids", lambda url, preset, cancel: listed.update(preset=preset) or
                        ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"])
    a = svc()
    r = a.start("download", mediadl.LIKED_MUSIC_URL, {"preset": "biliked"})
    assert r["ok"] and a._queue[-1]["kind"] == "expand"
    item = a._queue.pop()
    assert a._run_expand(item) == "ok"
    assert listed["preset"]["id"] == "biliked"                      # cookies only to read the list
    kids = [i for i in a._queue if i["kind"] == "media"]
    assert [k["url"][-11:] for k in kids] == ["aaaaaaaaaaa", "ccccccccccc"]   # archived one skipped
    assert all(k["preset"]["id"] == "bimusic" for k in kids)       # downloads use «Música» (cookies only if asked)


def test_max_downloads_reaches_yt_dlp(tmp_path):
    p = mediadl.clean_preset(mediadl.new_preset("x"))
    args = mediadl.build_args({**p, "_max_downloads": 5}, "https://youtu.be/x", tmp_path, with_cookies=False)
    assert args[args.index("--max-downloads") + 1] == "5"


def test_queueing_a_link_tells_the_window(svc, monkeypatch):
    no_worker(monkeypatch)
    events = []
    a = svc(events.append)
    a.start("download", "https://www.youtube.com/watch?v=aaaaaaaaaaa", {"preset": "bimusic"})
    q = [e for e in events if e["kind"] == "queue"]
    assert q and q[-1]["total"] == 1


def test_a_failed_song_says_which_and_why(svc, monkeypatch):
    # 0.9.2: the toast only said «Ocurrió un error» and the queue went on
    def fake_download(urls, preset, root, emit, cancel, **kw):
        st = mediadl.MediaStats(urls=1, failed=1)
        st.errors = [mediadl.friendly_error("[youtube] aaaaaaaaaaa: Video unavailable. This video has been removed")]
        return st
    monkeypatch.setattr(mediadl, "download", fake_download)
    no_worker(monkeypatch)
    a = svc()
    pushed = []
    monkeypatch.setattr(a, "_push", pushed.append)
    a.start("download", "https://www.youtube.com/watch?v=aaaaaaaaaaa", {"preset": "bimusic"})
    item = a._queue.pop(0)
    assert a._run_media(item) == "error"
    msg = next(e["message"] for e in pushed if e.get("kind") == "finished")
    assert "no se pudo bajar" in msg and "Video unavailable" in msg


def test_each_song_shows_while_a_long_list_downloads(svc, monkeypatch, tmp_path):
    # a friend's report (2026-11-06): many songs downloaded and none showed until Bakin restarted.
    # A playlist is one job; the library must be read again as each song lands, not at the end.
    events, during = [], []

    def fake_download(urls, preset, root, emit, cancel, **kw):
        out = mediadl.destination(preset, root)
        out.mkdir(parents=True, exist_ok=True)
        (out / mediadl.MARKER).write_text("{}", encoding="utf-8")
        for n in range(3):
            (out / f"Tema {n}.opus").write_bytes(b"x")
            emit({"kind": "media_file", "id": str(n), "title": f"Tema {n}", "path": str(out / f"Tema {n}.opus")})
            during.append(len(a.library_items({"kinds": ["audio"]})["items"]))
        return mediadl.MediaStats(urls=1, done=3)
    monkeypatch.setattr(mediadl, "download", fake_download)
    no_worker(monkeypatch)
    a = svc()
    a._push = events.append
    a.library_items({"kinds": ["audio"]})  # the index is built before the job, empty
    a.start("download", "https://www.youtube.com/playlist?list=PLx", {"preset": "bimusic"})
    a._run_media(a._queue.pop(0))
    assert during == [1, 2, 3]
    assert [e["kind"] for e in events].count("media_file") == 3


def test_music_search_opens_youtube_music_only(svc, monkeypatch):
    opened = []
    monkeypatch.setattr(service.webbrowser, "open", opened.append)
    a = svc()
    assert a.music_search_open("  Anuel   AA & co ")["ok"]
    assert opened == ["https://music.youtube.com/search?q=Anuel%20AA%20%26%20co"]
    assert not a.music_search_open("   ")["ok"] and len(opened) == 1


def test_follow_history(svc, monkeypatch):
    # PLAN 0.20.2: each check of a followed list keeps what came, what was there and why not
    def fake_download(urls, preset, root, emit, cancel, **kw):
        st = mediadl.MediaStats(urls=1, done=2, skipped=1, failed=1)
        st.errors = ["ERROR: [youtube] xyz: Private video. Sign in if you've been granted access"]
        return st
    monkeypatch.setattr(mediadl, "download", fake_download)
    no_worker(monkeypatch)
    a = svc()
    url = "https://www.youtube.com/playlist?list=PLabc"
    a._save_follows([{"id": "f1", "kind": "media", "url": url, "every_hours": 24, "last_check": 0, "enabled": True}])
    for _ in range(32):
        a.start("download", url, {"preset": "bimusic"})
        a._run_media(a._queue.pop(0))
    h = a._load_follows()[0]["history"]
    assert len(h) == 30 and h[-1]["new"] == 2 and h[-1]["skipped"] == 1
    assert "privado" in h[-1]["failed"][0].lower() or "private" in h[-1]["failed"][0].lower()
