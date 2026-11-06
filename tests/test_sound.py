"""PLAN 0.16 through the engine: gains for the player, lyrics local and from LRCLIB (faked)."""

import pytest

from bakin import loudness, lyrics, service, tools


@pytest.fixture
def svc(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "missing", lambda required=None: [])
    monkeypatch.setattr(service.Service, "_ensure_worker", lambda self: None)
    service.save_settings({**service.load_settings(), "out_dir": str(tmp_path / "dl")})
    s = service.Service()
    song = tmp_path / "Artista - Tema.opus"
    song.write_bytes(b"x")
    monkeypatch.setattr(service.Service, "_songs_by_id", lambda self, ids: {
        "s1": {"id": "s1", "path": str(song), "name": song.name, "title": "Tema", "artist": "Artista", "album": "Disco",
               "duration": 200.0, "size": 1, "mtime": 1}})
    return s


def test_gains_only_with_the_mode_on(svc, monkeypatch):
    monkeypatch.setattr(loudness, "load", lambda: {"s1": {"lufs": -12.0, "peak_db": -2.0, "size": 1, "mtime": 1}})
    assert svc.music_gains()["gains"] == {}
    svc.settings_set({"loudness_mode": "track"})
    assert svc.music_gains()["gains"] == {"s1": -6.0}  # toward -18 LUFS; the peak allows it


def test_lyrics_local_then_lrclib(svc, monkeypatch):
    r = svc.music_lyrics("s1")
    assert r["ok"] and not r["found"] and not r["online"]
    assert not svc.music_lyrics_fetch("s1")["ok"]  # off by default
    svc.settings_set({"lyrics_online": True})
    sent = {}
    monkeypatch.setattr(lyrics, "lookup", lambda t, a, al, d: sent.update(t=t, a=a) or {"lrc": "[00:01.00]hola\n[00:02.50]chau", "plain": "", "instrumental": False, "source": "lrclib"})
    r = svc.music_lyrics_fetch("s1")
    assert r["found"] and r["synced"] == [[1.0, "hola"], [2.5, "chau"]] and sent == {"t": "Tema", "a": "Artista"}
    assert svc.music_lyrics("s1")["synced"][1] == [2.5, "chau"]  # kept next to the song
