"""PLAN 0.23.4: without a connection every network feature answers in plain words, never a
traceback or an English exception."""

import urllib.error
import urllib.request

import pytest
import requests

from bakin import service


@pytest.fixture
def offline(monkeypatch):
    def refuse(*a, **kw):
        raise requests.ConnectionError("[Errno 11001] getaddrinfo failed")
    monkeypatch.setattr(requests.Session, "request", refuse)
    monkeypatch.setattr(requests, "get", refuse)
    monkeypatch.setattr(requests, "post", refuse)

    def refuse_urllib(*a, **kw):
        raise urllib.error.URLError("[Errno 11001] getaddrinfo failed")
    monkeypatch.setattr(urllib.request, "urlopen", refuse_urllib)  # Wallhaven's client


@pytest.mark.parametrize("call", [
    lambda s: s.wallhaven_search({"q": "mountains"}),
    lambda s: s.catalog_search("daft punk"),
    lambda s: s.spotify_resolve("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"),
])
def test_no_connection_is_said_plainly(offline, call):
    res = call(service.Service(lambda e: None))
    assert res.get("ok") is False, res
    err = res.get("error") or ""
    assert err and "Traceback" not in err and "getaddrinfo" not in err.split("(")[0], err
    assert any(w in err.lower() for w in ("conectar", "conexión", "internet")), err
    assert "actualiza bakin" not in err.lower(), err  # not a version problem
