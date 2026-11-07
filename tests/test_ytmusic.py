"""YouTube Music's catalogue (PLAN 0.15.1), with a fake ytmusicapi client: no network."""

import time

import pytest

from bakin import service, tools, ytmusic


class FakeYT:
    def __init__(self):
        self.calls = 0

    def search(self, q, filter, limit, ignore_spelling):
        self.calls += 1
        if q == "bloqueo":
            raise Exception("Server returned HTTP 429: Too Many Requests.")
        if filter == "songs":
            return [{"resultType": "song", "videoId": "aaaaaaaaaaa", "title": "Tema", "artists": [{"name": "Anuel AA"}],
                     "album": {"name": "Disco"}, "duration_seconds": 160, "isExplicit": True,
                     "videoType": "MUSIC_VIDEO_TYPE_ATV",
                     "thumbnails": [{"url": "https://yt3.googleusercontent.com/x=w60-h60-l90-rj"}]},
                    {"resultType": "song", "title": "sin id"}]
        if filter == "albums":
            return [{"resultType": "album", "browseId": "MPREb_x", "title": "Disco", "type": "Album", "year": "2022",
                     "artists": [{"name": "Anuel AA"}], "playlistId": "OLAK5uy_x", "thumbnails": []}]
        return []

    def get_album(self, browse_id):
        self.calls += 1
        return {"title": "Disco", "artists": [{"name": "Anuel AA"}], "year": "2022", "audioPlaylistId": "OLAK5uy_x",
                "thumbnails": [{"url": "https://yt3.googleusercontent.com/c=w226-h226"}],
                "tracks": [{"videoId": "bbbbbbbbbbb", "title": "Uno", "artists": [{"name": "Anuel AA"}], "thumbnails": None}]}


@pytest.fixture
def yt(monkeypatch):
    ytmusic.reset()
    fake = FakeYT()
    monkeypatch.setattr(ytmusic, "_client", fake)
    return fake


@pytest.fixture
def svc(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "missing", lambda required=None: [])
    monkeypatch.setattr(service.Service, "_ensure_worker", lambda self: None)
    service.save_settings({**service.load_settings(), "out_dir": str(tmp_path / "dl")})
    return service.Service()


def test_search_makes_cards_and_caches(yt):
    cards = ytmusic.search("anuel", "songs")
    assert cards == [{"kind": "song", "id": "aaaaaaaaaaa", "title": "Tema", "artist": "Anuel AA", "album": "Disco",
                      "duration": 160, "explicit": True, "available": True, "official": True,
                      "thumb": "https://yt3.googleusercontent.com/x=w300-h300-l90-rj"}]
    ytmusic.search("  ANUEL ", "songs")
    assert yt.calls == 1  # the same search, from the cache


def test_the_hourly_budget(yt):
    for n in range(3):
        ytmusic.search(f"q{n}", "songs", hourly=3)
    with pytest.raises(ytmusic.Busy) as e:
        ytmusic.search("otra", "songs", hourly=3)
    assert "3 búsquedas por hora" in str(e.value) and e.value.until > time.time()
    ytmusic.search("q0", "songs", hourly=3)  # cached: free


def test_youtube_pushback_stops_the_catalogue_for_an_hour(yt):
    with pytest.raises(ytmusic.Busy):
        ytmusic.search("bloqueo", "songs")
    with pytest.raises(ytmusic.Busy):
        ytmusic.search("otra cosa", "songs")
    assert yt.calls == 1  # no second request while paused


def test_album_page_and_its_link(yt):
    a = ytmusic.album("MPREb_x")
    assert a["tracks"][0]["id"] == "bbbbbbbbbbb" and a["tracks"][0]["thumb"]  # the album's cover
    assert ytmusic.link({"kind": "album", "id": "MPREb_x"}) == "https://music.youtube.com/playlist?list=OLAK5uy_x"
    assert ytmusic.link({"kind": "song", "id": "aaaaaaaaaaa"}) == "https://music.youtube.com/watch?v=aaaaaaaaaaa"


def test_service_marks_what_you_have_and_queues(svc, yt, monkeypatch):
    monkeypatch.setattr(service.Service, "_have_ids", lambda self: {"aaaaaaaaaaa"})
    r = svc.catalog_search("anuel", "songs")
    assert r["ok"] and r["items"][0]["have"] is True
    queued = []
    monkeypatch.setattr(service.Service, "start", lambda self, mode, url, opts=None: queued.append(url) or {"ok": True})
    res = svc.catalog_download([{"kind": "song", "id": "aaaaaaaaaaa"}, {"kind": "album", "id": "MPREb_x", "playlist": "OLAK5uy_x"}])
    assert res["queued"] == 2 and queued[1].endswith("list=OLAK5uy_x")


def test_service_says_why_it_stopped(svc, yt):
    r = svc.catalog_search("bloqueo", "songs")
    assert not r["ok"] and "esperar" in r["error"] and r["until"]


def test_thumbs_only_from_youtube_hosts(tmp_path):
    class S:
        def get(self, url, timeout):
            raise AssertionError("no request expected")
    assert ytmusic.thumbs(["https://evil.example/x.jpg", "http://yt3.googleusercontent.com/x"], session=S()) == {}


def test_settings_only_take_known_keys(svc):
    r = svc.settings_set({"catalog_hourly": "5000", "catalog_covers": 0, "out_dir": "C:/hack"})
    assert r["ok"] and r["settings"]["catalog_hourly"] == 1000 and r["settings"]["catalog_covers"] is False
    assert svc.settings_set({"loudness_mode": "loud", "loudness_preamp": 9})["settings"]["loudness_mode"] == "off"
    assert svc.settings_set({"loudness_preamp": 9})["settings"]["loudness_preamp"] == 6.0
    assert service.load_settings()["out_dir"] != "C:/hack"


class PagesYT:
    """An artist's and a playlist's pages, and a client that breaks in an unknown way."""

    def __init__(self):
        self.calls = 0

    def get_artist(self, browse_id):
        self.calls += 1
        return {"name": "Anuel AA", "subscribers": "10 M", "thumbnails": [{"url": "https://lh3.googleusercontent.com/a"}],
                "songs": {"browseId": "VLsongs", "results": [{"videoId": "ccccccccccc", "title": "Uno", "artists": "Anuel AA"}]},
                "albums": {"results": [{"browseId": "MPREb_a", "title": "Disco"}, {"title": "sin id"}]},
                "singles": {"results": [{"browseId": "MPREb_s", "title": "Single", "type": "Single"}]}}

    def get_playlist(self, playlist_id, limit):
        self.calls += 1
        if playlist_id == "roto":
            raise Exception("KeyError: 'contents'")
        return {"title": "Lista", "author": [{"name": "Alguien"}], "thumbnails": [],
                "tracks": [{"videoId": "ddddddddddd", "title": "Dos", "isAvailable": False}, {"title": "sin id"}]}


@pytest.fixture
def pages(monkeypatch):
    ytmusic.reset()
    fake = PagesYT()
    monkeypatch.setattr(ytmusic, "_client", fake)
    yield fake
    ytmusic.reset()


def test_artist_page(pages):
    a = ytmusic.artist("UC_x")
    assert a["title"] == "Anuel AA" and a["subscribers"] == "10 M" and a["songs_list"] == "VLsongs"
    assert [s["id"] for s in a["songs"]] == ["ccccccccccc"] and a["songs"][0]["artist"] == "Anuel AA"
    assert [x["id"] for x in a["albums"]] == ["MPREb_a", "MPREb_s"] and a["albums"][0]["type"] == "Álbum"


def test_playlist_page_and_link(pages):
    p = ytmusic.playlist("PL_x")
    assert p["title"] == "Lista" and p["artist"] == "Alguien" and p["count"] == 1
    assert p["tracks"][0]["available"] is False
    assert ytmusic.link({"kind": "playlist", "id": "PL_x"}) == "https://music.youtube.com/playlist?list=PL_x"
    assert ytmusic.link({"kind": "artist", "id": "UC_x"}) == ""


def test_an_unknown_failure_is_not_a_pause(pages):
    with pytest.raises(ytmusic.CatalogError) as e:
        ytmusic.playlist("roto")
    assert not isinstance(e.value, ytmusic.Busy)
    assert ytmusic.playlist("PL_x")["title"] == "Lista"  # the catalogue keeps working


@pytest.mark.parametrize("r,kind,want", [
    ({"resultType": "artist", "browseId": "UC_a", "artist": "Nombre"}, None, {"kind": "artist", "id": "UC_a", "title": "Nombre", "thumb": ""}),
    ({"resultType": "playlist", "browseId": "VLPL_a", "title": "L", "author": "Yo", "itemCount": "12"}, None,
     {"kind": "playlist", "id": "PL_a", "title": "L", "artist": "Yo", "count": "12", "thumb": ""}),
    ({"resultType": "episode", "videoId": "x"}, None, None),
    ({"resultType": "artist"}, None, None),  # no id: nothing to open
])
def test_cards_of_every_kind(r, kind, want):
    assert ytmusic.card(r, kind) == want


def test_search_edge_cases(yt):
    assert ytmusic.search("   ", "songs") == [] and yt.calls == 0
    assert ytmusic.search("anuel", "nonsense")[0]["kind"] == "song"  # an unknown kind: songs


def test_the_cache_forgets_the_oldest(yt):
    for n in range(201):
        ytmusic.search(f"q{n}", "videos", hourly=0)
    assert len(ytmusic._cache) == 151
    assert ("search", "q0", "songs") not in ytmusic._cache and ("search", "q200", "songs") in ytmusic._cache


def test_the_test_copy_reads_a_saved_catalogue(monkeypatch, tmp_path):
    import json
    f = tmp_path / "catalog.json"
    f.write_text(json.dumps({"search": {"songs": [{"videoId": "eeeeeeeeeee", "title": "Grabado"}]},
                             "album": {"title": "A"}, "artist": {"name": "B"}}), encoding="utf-8")
    monkeypatch.setenv("BAKIN_FAKE_YTMUSIC", str(f))
    ytmusic.reset()
    try:
        assert ytmusic.search("x", "songs")[0]["title"] == "Grabado"
        assert ytmusic.album("MPREb_y")["title"] == "A" and ytmusic.artist("UC_y")["title"] == "B"
        assert ytmusic.playlist("PL_y")["tracks"] == []
    finally:
        ytmusic.reset()
