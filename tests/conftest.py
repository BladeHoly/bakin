"""Shared fixtures."""

import io

import pytest
from PIL import Image

from bakin import redact


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("BAKIN_HOME", str(tmp_path / "apphome"))
    redact.clear_secrets()
    # never the real Windows Credential Manager: a dict per test
    from bakin import credstore, wallhaven
    vault: dict[str, str] = {}
    monkeypatch.setattr(credstore, "get", lambda t: vault.get(t))
    monkeypatch.setattr(credstore, "put", lambda t, s, user="Bakin": vault.__setitem__(t, s))
    monkeypatch.setattr(credstore, "delete", lambda t: vault.pop(t, None))
    wallhaven.forget_key_cache()
    # module caches keyed by ids that repeat between tests (each test has its own BAKIN_HOME)
    from bakin import audiotags, music
    music.reset_memory_cache()
    audiotags.reset_memory_cache()
    yield
    wallhaven.forget_key_cache()
    redact.clear_secrets()


def jpeg_bytes(w, h, color=(120, 30, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "JPEG", quality=80)
    return buf.getvalue()
