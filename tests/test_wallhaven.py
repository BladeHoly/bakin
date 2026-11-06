"""Wallhaven: filters (never NSFW), links, downloads with tags, the queue.
The site is faked: no test talks to wallhaven.cc."""

import io
import json
import threading
import urllib.error

import pytest

from bakin import library, service, tools, wallhaven


@pytest.fixture
def svc(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "missing", lambda required=None: [])
    service.save_settings({**service.load_settings(), "out_dir": str(tmp_path / "dl")})
    return service.Service


def wp(wid, purity="sfw"):
    return {"id": wid, "url": f"https://wallhaven.cc/w/{wid}", "path": f"https://w.wallhaven.cc/full/{wid[:2]}/wallhaven-{wid}.jpg",
            "thumbs": {"large": f"https://th.wallhaven.cc/lg/{wid[:2]}/{wid}.jpg", "small": "s", "original": "o"},
            "dimension_x": 3840, "dimension_y": 2160, "resolution": "3840x2160", "ratio": "1.78",
            "file_size": 5, "file_type": "image/jpeg", "purity": purity, "category": "general", "colors": ["000000"]}


class FakeSite:
    """Answers like the API: two pages of search results, info with tags, files."""

    def __init__(self, pages, status=None):
        self.pages = pages
        self.status = status or {}
        self.urls = []

    def __call__(self, url, timeout=30, key=None):
        self.urls.append(url)
        self.keys = [*getattr(self, "keys", []), key]
        for part, code in self.status.items():
            if part in url:
                raise wallhaven.Blocked("429") if code == 429 else wallhaven.WallhavenError(str(code))
        if "/api/v1/search" in url:
            page = int(url.split("page=")[1].split("&")[0]) if "page=" in url else 1
            body = {"data": self.pages[page - 1], "meta": {"current_page": page, "last_page": len(self.pages),
                                                        "per_page": 24, "total": sum(map(len, self.pages)), "seed": None}}
        elif "/api/v1/w/" in url:
            wid = url.rsplit("/", 1)[1]
            body = {"data": {**wp(wid), "uploader": {"username": "someone"},
                             "tags": [{"name": "space"}, {"name": "dark  sky"}, {"name": "space"}]}}
        elif "/api/v1/settings" in url:
            if key != "A" * 32:
                raise wallhaven.WallhavenError("401")
            body = {"data": {"purity": ["sfw"]}}
        elif "/api/v1/collections" in url and url.endswith("/collections"):
            body = {"data": [{"id": 15, "label": "Mis fondos", "count": 7, "public": 0}, {"id": None}]}
        elif url.startswith("https://w.wallhaven.cc/"):
            return io.BytesIO(b"JPEG!")
        else:
            raise AssertionError(url)
        return io.BytesIO(json.dumps(body).encode())


@pytest.fixture
def site(monkeypatch):
    monkeypatch.setattr(wallhaven, "MIN_GAP", 0)

    def install(pages, status=None):
        fake = FakeSite(pages, status)
        monkeypatch.setattr(wallhaven, "_open", fake)
        return fake
    return install


def test_never_nsfw_and_sketchy_only_when_asked():
    assert wallhaven.clean_filters({})["purity"] == "100"
    assert wallhaven.clean_filters({"purity": "111"})["purity"] == "110"  # nsfw bit dropped
    assert wallhaven.clean_filters({"purity": "101"})["purity"] == "100"
    assert wallhaven.clean_filters({"sketchy": True})["purity"] == "110"
    f = wallhaven.clean_filters({"q": "  space   cat ", "categories": ["anime"], "sorting": "nope", "atleast": "2560x1440",
                                 "ratios": ["16x9", "9x16", "bad"], "colors": "#000000"})
    assert f == {"q": "space cat", "categories": "010", "purity": "100", "sorting": "toplist", "order": "desc",
                 "topRange": "1M", "atleast": "2560x1440", "ratios": "16x9,9x16", "colors": "000000"}


def test_search_drops_nsfw_even_if_the_site_sends_it(site):
    site([[wp("aaaa1"), wp("bbbb2", "nsfw"), wp("cccc3", "sketchy")]])
    res = wallhaven.search({"sketchy": True})
    assert [i["id"] for i in res["items"]] == ["aaaa1", "cccc3"]
    assert res["items"][0]["thumb"].startswith("https://th.wallhaven.cc/")


@pytest.mark.parametrize("url,expected", [
    ("https://wallhaven.cc/w/94x38z", {"type": "wallpaper", "id": "94x38z"}),
    ("whvn.cc/94x38z", {"type": "wallpaper", "id": "94x38z"}),
    ("https://w.wallhaven.cc/full/94/wallhaven-94x38z.jpg", {"type": "wallpaper", "id": "94x38z"}),
    ("https://wallhaven.cc/user/Someone/favorites/123", {"type": "collection", "user": "Someone", "id": "123"}),
    ("https://example.com/w/94x38z", None),
])
def test_links(url, expected):
    assert wallhaven.parse_url(url) == expected


def test_a_search_link_keeps_its_filters_but_never_nsfw():
    got = wallhaven.parse_url("https://wallhaven.cc/search?q=space&categories=110&purity=111&sorting=views")
    assert got["type"] == "search"
    assert got["filters"]["q"] == "space" and got["filters"]["categories"] == "110"
    assert got["filters"]["purity"] == "110" and got["filters"]["sorting"] == "views"


def test_download_saves_files_with_their_tags_and_never_twice(site, tmp_path):
    site([[wp("aaaa1"), wp("bbbb2")], [wp("cccc3")]])
    events = []
    job = {"type": "search", "filters": wallhaven.clean_filters({"q": "space"}), "limit": 10, "label": "space"}
    st = wallhaven.download(job, tmp_path, events.append, threading.Event())
    assert (st.done, st.skipped, st.failed) == (3, 0, 0)
    dest = tmp_path / "Wallhaven"
    assert (dest / "space" / "wallhaven-aaaa1.jpg").read_bytes() == b"JPEG!"
    assert (dest / ".bakin-media.json").is_file()  # the library lists it as a collection
    col = library.Collection("md:Wallhaven", "media", "Wallhaven", dest)
    assert library.Meta(col).get("space/wallhaven-aaaa1.jpg")["tags"] == ["space", "dark sky"]
    assert any(e["kind"] == "wallhaven_progress" for e in events)
    again = wallhaven.download(job, tmp_path, events.append, threading.Event())
    assert (again.done, again.skipped) == (0, 3)
    items = library.scan(col)
    assert sorted(i["name"] for i in items) == ["wallhaven-aaaa1.jpg", "wallhaven-bbbb2.jpg", "wallhaven-cccc3.jpg"]
    assert all(i["kind"] == "image" for i in items)


def test_a_limit_stops_the_search_early(site, tmp_path):
    fake = site([[wp("aaaa1"), wp("bbbb2")], [wp("cccc3")]])
    st = wallhaven.download({"type": "search", "filters": {}, "limit": 2}, tmp_path, lambda e: None, threading.Event())
    assert st.done == 2
    assert not any("page=2" in u for u in fake.urls)


def test_429_stops_and_says_so(site, tmp_path):
    site([[wp("aaaa1"), wp("bbbb2")]], status={"/w/bbbb2": 429})
    st = wallhaven.download({"type": "search", "filters": {}, "limit": 5}, tmp_path, lambda e: None, threading.Event())
    assert st.done == 1 and st.blocked


def test_http_429_becomes_blocked(monkeypatch):
    monkeypatch.setattr(wallhaven, "MIN_GAP", 0)

    def boom(req, timeout=30):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)
    monkeypatch.setattr(wallhaven.urllib.request, "urlopen", boom)
    with pytest.raises(wallhaven.Blocked):
        wallhaven.search({})


def test_pasting_a_link_queues_a_job_that_survives_a_restart(svc, monkeypatch):
    monkeypatch.setattr(service.Service, "_ensure_worker", lambda self: None)
    a = svc()
    assert a.start("download", "https://wallhaven.cc/w/94x38z")["ok"]
    assert not a.start("download", "https://wallhaven.cc/w/94x38z")["ok"]  # already queued
    assert a.wallhaven_download({"type": "ids", "ids": ["aaaa1", "bad id!", "aaaa1"], "label": "elegidos"})["ok"]
    a._save_queue(force=True)
    b = svc()
    jobs = [i["job"] for i in b._queue]
    assert jobs[0] == {"type": "wallpaper", "id": "94x38z", "label": "Fondos sueltos"}
    assert jobs[1] == {"type": "ids", "ids": ["aaaa1"], "label": "elegidos"}


def test_open_only_opens_a_wallhaven_page(svc, monkeypatch):
    opened = []
    monkeypatch.setattr(service.webbrowser, "open", opened.append)
    a = svc()
    assert a.wallhaven_open("94x38z")["ok"]
    assert not a.wallhaven_open("https://evil.example")["ok"]
    assert opened == ["https://wallhaven.cc/w/94x38z"]


def test_the_job_runs_and_tells_the_window(svc, site, monkeypatch):
    site([[wp("aaaa1")]])
    monkeypatch.setattr(service.Service, "_ensure_worker", lambda self: None)
    a = svc()
    pushed = []
    monkeypatch.setattr(a, "_push", pushed.append)
    a.start("download", "https://wallhaven.cc/search?q=space")
    item = a._queue.pop(0)
    assert a._run_wallhaven(item) == "ok"
    done = next(e for e in pushed if e["kind"] == "finished")
    assert done["outcome"] == "ok" and "1 fondo nuevo" in done["message"]


# ------------------------------------------------- optional API key (PLAN 0.13.4)

def test_the_key_goes_only_to_the_api_and_never_to_the_image_servers(monkeypatch):
    from bakin import credstore
    seen = []

    class R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout=30):
        seen.append((req.full_url, req.get_header("X-api-key")))
        return R(b"{}")

    monkeypatch.setattr(wallhaven, "MIN_GAP", 0)
    monkeypatch.setattr(wallhaven.urllib.request, "urlopen", urlopen)
    wallhaven._get("/search", {"q": "x"})
    assert seen[-1][1] is None  # no key saved: none sent
    credstore.put(wallhaven.key_target(), "K" * 32)
    wallhaven.forget_key_cache()
    wallhaven._get("/search", {"q": "x"})
    wallhaven._open("https://w.wallhaven.cc/full/ab/wallhaven-abcd12.jpg")
    assert seen[-2] == (f"{wallhaven.API}/search?q=x", "K" * 32)
    assert seen[-1][1] is None
    # and it is scrubbed from anything logged
    from bakin import redact
    assert "K" * 32 not in redact.redact(f"GET /search X-API-Key: {'K' * 32}")
    assert "K" * 32 not in redact.redact(f"?apikey={'K' * 32}&q=x")


def test_account_key_checked_saved_and_cleared(monkeypatch):
    from bakin import credstore
    fake = FakeSite([[]])
    monkeypatch.setattr(wallhaven, "MIN_GAP", 0)
    monkeypatch.setattr(wallhaven, "_open", fake)
    svc = service.Service()
    assert svc.wallhaven_account() == {"key": False, "user": ""}
    bad = svc.wallhaven_account_set("short", "yo")
    assert not bad["ok"] and bad["field"] == "key"
    wrong = svc.wallhaven_account_set("B" * 32, "yo")
    assert not wrong["ok"] and credstore.get(wallhaven.key_target()) is None
    ok = svc.wallhaven_account_set("A" * 32, "yo")
    assert ok == {"ok": True, "key": True, "user": "yo"}
    assert credstore.get(wallhaven.key_target()) == "A" * 32
    assert "A" * 32 not in json.dumps(service.load_settings())  # the key never in a file
    cols = svc.wallhaven_my_collections()
    assert cols["collections"] == [{"id": "15", "label": "Mis fondos", "count": 7, "public": False}]
    assert svc.wallhaven_account_clear()["key"] is False
    assert not svc.wallhaven_my_collections()["ok"]


def test_following_a_search_queues_it_now_and_when_due(monkeypatch, tmp_path):
    import time
    svc = service.Service()
    svc.opts = {"out_dir": str(tmp_path / "dl")}
    queued = []
    monkeypatch.setattr(service.Service, "wallhaven_download", lambda self, job=None: queued.append(job) or {"ok": True})
    r = svc.wallhaven_follow({"type": "search", "filters": {"q": "nebula", "sorting": "date_added"}, "label": "Nebulosas"}, 6)
    assert r["ok"] and r["queued"] and queued[-1]["label"] == "Nebulosas"
    assert not svc.wallhaven_follow({"type": "search", "filters": {"q": "nebula", "sorting": "date_added"}, "label": "Nebulosas"})["ok"]
    assert not svc.wallhaven_follow({"type": "wallpaper", "id": "abcd12"})["ok"]
    assert [f["kind"] for f in svc.follows_list()["follows"]] == ["wallhaven"]
    # not due again until 6 h later
    assert svc._check_follows(time.time() + 60) == 0
    assert svc._check_follows(time.time() + 6 * 3600 + 60) == 1 and len(queued) == 2


def test_a_test_copy_never_uses_the_real_credential(monkeypatch):
    monkeypatch.delenv("BAKIN_HOME")
    assert wallhaven.key_target() == "bakin-wallhaven"
    monkeypatch.setenv("BAKIN_HOME", r"C:\tmp\copia")
    assert wallhaven.key_target().startswith("bakin-wallhaven-test-")
