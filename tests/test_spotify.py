"""Spotify → YouTube Music (PLAN 0.15.2): reading a Spotify page or CSV, and the match score.
No network: a saved page (shape of Spotify's embed data, made-up songs) and fake search results."""

from pathlib import Path

import pytest

from bakin import matching, service, spotify, tools, ytmusic

PAGE = (Path(__file__).parent / "fixtures" / "spotify" / "album.html").read_text(encoding="utf-8")


@pytest.mark.parametrize("text,want", [
    ("https://open.spotify.com/album/4aawyAB9vmqN3uQ7FjRGTy?si=x", ("album", "4aawyAB9vmqN3uQ7FjRGTy")),
    ("https://open.spotify.com/intl-es/track/6OmhkSOpvYBokMKQxpIGx2", ("track", "6OmhkSOpvYBokMKQxpIGx2")),
    ("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M", ("playlist", "37i9dQZF1DXcBWIGoYBM5M")),
    ("https://open.spotify.com/embed/playlist/37i9dQZF1DXcBWIGoYBM5M", ("playlist", "37i9dQZF1DXcBWIGoYBM5M")),
    ("https://www.youtube.com/watch?v=x", None),
])
def test_links(text, want):
    assert spotify.parse_link(text) == want


def test_page():
    d = spotify.from_page(PAGE)
    assert d["kind"] == "album" and d["name"] == "Disco de prueba"
    assert d["tracks"][0] == {"title": "Tema Uno (feat. Invitada)", "artists": ["Artista", "Invitada"],
                              "duration": 200.5, "explicit": True, "album": "Disco de prueba"}
    with pytest.raises(spotify.SpotifyError):
        spotify.from_page("<html>nada</html>")


def test_csv_from_exportify():
    text = "﻿Track URI,Track Name,Artist Name(s),Album Name,Duration (ms)\nx,Tema,\"A, B\",Disco,200000\nx,,C,,1\n"
    d = spotify.from_csv(text)
    assert d["tracks"] == [{"title": "Tema", "artists": ["A", "B"], "duration": 200.0, "explicit": False, "album": "Disco"}]
    with pytest.raises(spotify.SpotifyError):
        spotify.from_csv("a,b\n1,2\n")


def card(title, artist, duration, official=True, explicit=False, vid="v"):
    return {"kind": "song", "id": vid, "title": title, "artist": artist, "duration": duration,
            "official": official, "explicit": explicit, "available": True}


def test_score_sure_doubt_and_no():
    t = {"title": "Tema Uno (feat. Invitada)", "artists": ["Artista", "Invitada"], "duration": 200.5, "explicit": True}
    assert matching.score(t, card("Tema Uno (feat. Invitada)", "Artista", 201, explicit=True))["verdict"] == "sure"
    assert matching.score(t, card("Tema Uno", "Artista", 240))["verdict"] == "no"  # 40 s off
    live = matching.score(t, card("Tema Uno (Live)", "Artista", 202, explicit=True))
    assert live["verdict"] != "sure" and "live" in live["why"]
    assert matching.score(t, card("Otra Cosa", "Nadie", 200))["verdict"] == "no"


def test_best_keeps_alternatives():
    t = {"title": "Tema Dos", "artists": ["Artista"], "duration": 180, "explicit": False}
    b = matching.best(t, [card("Tema Dos (Live)", "Artista", 181, vid="a"), card("Tema Dos", "Artista", 180, vid="b"),
                          card("Tema Dos", "Otro", 300, vid="c")])
    assert b["match"]["id"] == "b" and b["verdict"] == "sure" and [c["id"] for c in b["alternatives"]][0] == "a"


def test_service_resolves_in_the_background(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "missing", lambda required=None: [])
    monkeypatch.setattr(service.Service, "_ensure_worker", lambda self: None)
    service.save_settings({**service.load_settings(), "out_dir": str(tmp_path / "dl")})
    monkeypatch.setattr(spotify, "fetch", lambda url: spotify.from_page(PAGE))
    monkeypatch.setattr(ytmusic, "search", lambda q, kind, hourly=0: [card("Tema Uno (feat. Invitada)", "Artista", 200, explicit=True, vid="x")])
    events = []
    s = service.Service(push=events.append)
    r = s.spotify_resolve("https://open.spotify.com/album/4aawyAB9vmqN3uQ7FjRGTy")
    assert r["ok"] and r["count"] == 2
    for _ in range(100):
        if any(e["kind"] == "sp_done" for e in events):
            break
        __import__("time").sleep(0.05)
    tracks = [e for e in events if e["kind"] == "sp_track"]
    assert [e["verdict"] for e in tracks] == ["sure", "no"]  # the remix isn't the same song
    assert events[-1]["found"] == 1


def test_csv_reader_only_reads_csv(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("hola", encoding="utf-8")
    assert not service.Service.read_text_file(str(f))["ok"]
    g = tmp_path / "x.csv"
    g.write_text("Track Name,Artist Name(s)\nA,B\n", encoding="utf-8")
    assert service.Service.read_text_file(str(g))["text"].startswith("Track Name")


def _page(entity) -> str:
    import json
    data = {"props": {"pageProps": {"state": {"data": {"entity": entity}}}}}
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'


def test_page_of_one_track_and_of_a_changed_page():
    d = spotify.from_page(_page({"type": "track", "name": "Solo", "artists": [{"name": "A"}, {"name": " "}],
                                 "duration": 0, "isExplicit": False}))
    assert d == {"kind": "track", "name": "Solo",
                 "tracks": [{"title": "Solo", "artists": ["A"], "duration": None, "explicit": False, "album": ""}]}
    pl = spotify.from_page(_page({"name": "Lista", "trackList": [{"title": "T", "subtitle": "X"}, {"title": ""}]}))
    assert pl["kind"] == "playlist" and [t["title"] for t in pl["tracks"]] == ["T"] and pl["tracks"][0]["album"] == ""
    with pytest.raises(spotify.SpotifyError, match="cambió"):  # the data moved inside the page
        spotify.from_page('<script id="__NEXT_DATA__" type="application/json">{"props": {}}</script>')


class FakeSession:
    def __init__(self, status=200, text="", exc=None):
        self.status, self.text, self.exc, self.urls = status, text, exc, []

    def get(self, url, timeout, headers):
        self.urls.append(url)
        if self.exc:
            raise self.exc
        return type("R", (), {"status_code": self.status, "text": self.text})()


def test_fetch_asks_the_embed_page_once(monkeypatch):
    monkeypatch.delenv("BAKIN_FAKE_SPOTIFY", raising=False)
    s = FakeSession(text=PAGE)
    d = spotify.fetch("https://open.spotify.com/album/4aawyAB9vmqN3uQ7FjRGTy", session=s)
    assert s.urls == ["https://open.spotify.com/embed/album/4aawyAB9vmqN3uQ7FjRGTy"]
    assert d["kind"] == "album" and d["limited"] is False
    hundred = _page({"type": "playlist", "name": "L", "trackList": [{"title": f"T{i}", "subtitle": "A"} for i in range(100)]})
    assert spotify.fetch("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M", session=FakeSession(text=hundred))["limited"] is True


@pytest.mark.parametrize("status,why", [(404, "privado"), (429, "esperar"), (503, "esperar"), (500, "500")])
def test_fetch_says_why_it_failed(monkeypatch, status, why):
    monkeypatch.delenv("BAKIN_FAKE_SPOTIFY", raising=False)
    with pytest.raises(spotify.SpotifyError, match=why):
        spotify.fetch("spotify:track:6OmhkSOpvYBokMKQxpIGx2", session=FakeSession(status=status))


def test_fetch_offline_and_not_a_link(monkeypatch):
    import requests
    monkeypatch.delenv("BAKIN_FAKE_SPOTIFY", raising=False)
    with pytest.raises(spotify.SpotifyError, match="conectar"):
        spotify.fetch("spotify:track:6OmhkSOpvYBokMKQxpIGx2", session=FakeSession(exc=requests.ConnectionError()))
    s = FakeSession()
    with pytest.raises(spotify.SpotifyError, match="no es un enlace"):
        spotify.fetch("https://example.com/x", session=s)
    assert s.urls == []


def test_fetch_reads_the_saved_page_in_a_test_copy(monkeypatch, tmp_path):
    f = tmp_path / "page.html"
    f.write_text(PAGE, encoding="utf-8")
    monkeypatch.setenv("BAKIN_FAKE_SPOTIFY", str(f))
    s = FakeSession(status=500)
    assert spotify.fetch("spotify:album:4aawyAB9vmqN3uQ7FjRGTy", session=s)["name"] == "Disco de prueba"
    assert s.urls == []  # never the network


def test_csv_edge_cases():
    with pytest.raises(spotify.SpotifyError, match="no tiene temas"):
        spotify.from_csv("Track Name,Artist Name(s)\n")
    with pytest.raises(spotify.SpotifyError, match="columnas"):
        spotify.from_csv("Song,Who\nA,B\n")
    d = spotify.from_csv("title,artists\nTema,A; B\n")  # other names for the columns; no length, no album
    assert d["tracks"] == [{"title": "Tema", "artists": ["A", "B"], "duration": None, "explicit": False, "album": ""}]
