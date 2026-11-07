"""The vault (DPAPI) and the cookies imported into it (2026-11-07). Fake cookies only, in tmp."""

import time

import pytest

from bakin import mediadl, service, tools, vault

FAKE = (
    "# Netscape HTTP Cookie File\n"
    f".youtube.com\tTRUE\t/\tTRUE\t{int(time.time()) + 86400 * 30}\tSID\tfake-not-a-real-value\n"
    f".youtube.com\tTRUE\t/\tTRUE\t{int(time.time()) + 86400 * 30}\tPREF\tf1=1\n"
)


@pytest.fixture
def svc(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "missing", lambda required=None: [])
    monkeypatch.setattr(service.Service, "_ensure_worker", lambda self: None)
    service.save_settings({**service.load_settings(), "out_dir": str(tmp_path / "dl")})
    return service.Service


def cookies_file(tmp_path, text=FAKE):
    p = tmp_path / "exported" / "cookies.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_vault_keeps_secrets_encrypted():
    vault.put("cookies-youtube", FAKE.encode())
    raw = (vault._dir() / "cookies-youtube.bin").read_bytes()
    assert b"fake-not-a-real-value" not in raw and b"SID" not in raw
    assert vault.get("cookies-youtube") == FAKE.encode()
    vault.delete("cookies-youtube")
    assert vault.get("cookies-youtube") is None and not vault.has("cookies-youtube")


def test_plain_copies_are_wiped():
    p = vault.scratch(b"secret")
    assert p.read_bytes() == b"secret"
    vault.wipe(p)
    assert not p.exists()
    left = vault.scratch(b"left by a crash")
    vault.sweep()
    assert not left.exists()


def test_import_switches_the_presets_and_never_returns_values(svc, tmp_path):
    src = cookies_file(tmp_path)
    ms = mediadl.load_settings()
    for p in ms["presets"]:
        if p["id"] == "biliked":
            p.update(cookies_source="file", cookies_file=str(src))
    mediadl.save_settings(ms)
    a = svc()
    res = a.cookies_import()  # no path: the one file the presets use
    assert res["ok"] and "fake-not-a-real-value" not in repr(res)
    assert res["status"]["youtube_session"] and res["status"]["vault"]
    liked = next(p for p in mediadl.load_settings()["presets"] if p["id"] == "biliked")
    assert liked["cookies_source"] == "vault" and liked["cookies_file"] == ""
    assert src.exists()  # the plain file stays until the user deletes it
    status = a.cookies_status()["cookies"]
    assert any(c.get("vault") and c["youtube_session"] for c in status)
    assert "fake-not-a-real-value" not in repr(a.cookies_vault())
    # then the plain one goes: overwritten and deleted, not to the Recycle Bin
    assert a.cookies_delete_original()["ok"] and not src.exists()
    assert mediadl.load_settings()["cookies_imported_from"] == ""


def test_a_file_without_a_youtube_session_is_refused(svc, tmp_path):
    src = cookies_file(tmp_path, "# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t0\tx\ty\n")
    res = svc().cookies_import(str(src))
    assert not res["ok"] and not vault.has(mediadl.VAULT_COOKIES)


def test_downloads_use_a_plain_copy_only_while_running_and_keep_renewed_cookies(monkeypatch, tmp_path):
    vault.put(mediadl.VAULT_COOKIES, FAKE.encode())
    preset = {**next(p for p in mediadl.default_presets() if p["id"] == "biliked"),
              "cookies_source": "vault", "cookies_mode": "always"}
    seen = {}

    def fake_retries(runner, preset, url, out_dir, cookies, retry, emit, cancel):
        args = mediadl.cookie_args(preset)
        path = args[1]
        seen["path"] = path
        seen["plain"] = open(path, encoding="utf-8").read() == FAKE
        with open(path, "a", encoding="utf-8") as fp:  # yt-dlp writes renewed cookies back
            fp.write(f".youtube.com\tTRUE\t/\tTRUE\t{int(time.time()) + 99999}\tSAPISID\trenewed\n")
        return {"done": [], "errors": [], "started": [], "n_entries": None, "cookies_expired": False}
    monkeypatch.setattr(mediadl, "_run_with_retries", fake_retries)
    mediadl.download(["https://music.youtube.com/playlist?list=LM"], preset, tmp_path / "dl",
                     lambda ev: None, __import__("threading").Event())
    assert seen["plain"]
    assert not __import__("os").path.exists(seen["path"])  # wiped when the run ended
    assert b"renewed" in vault.get(mediadl.VAULT_COOKIES)
    assert mediadl._vault_live["users"] == 0 and mediadl._vault_live["path"] is None


def test_forget_turns_the_vault_presets_off(svc, tmp_path):
    a = svc()
    src = cookies_file(tmp_path)
    ms = mediadl.load_settings()
    for p in ms["presets"]:
        if p["id"] == "biliked":
            p.update(cookies_source="file", cookies_file=str(src))
    mediadl.save_settings(ms)
    assert a.cookies_import(str(src))["ok"]
    assert a.cookies_forget()["ok"] and not vault.has(mediadl.VAULT_COOKIES)
    liked = next(p for p in mediadl.load_settings()["presets"] if p["id"] == "biliked")
    assert liked["cookies_mode"] == "off"


def test_use_vault_moves_the_presets_stuck_on_a_locked_browser(svc, tmp_path):
    # pulido D15: with cookies already saved, a preset on Brave still warned in orange
    a = svc()
    assert not a.cookies_use_vault()["ok"]  # nothing saved yet
    src = cookies_file(tmp_path)
    assert a.cookies_import(str(src))["ok"]
    ms = mediadl.load_settings()
    for p in ms["presets"]:
        p.update(cookies_source="browser", cookies_browser="brave", cookies_mode="auto")
    next(p for p in ms["presets"] if p["id"] == "bimusic").update(cookies_browser="firefox")
    mediadl.save_settings(ms)
    assert a.cookies_use_vault()["ok"]
    by_id = {p["id"]: p for p in mediadl.load_settings()["presets"]}
    assert by_id["biliked"]["cookies_source"] == "vault"
    assert by_id["bimusic"]["cookies_source"] == "browser"


def test_import_also_serves_presets_stuck_on_a_locked_browser(svc, tmp_path):
    # the user's presets used Brave, whose cookies yt-dlp can't read on Windows
    src = cookies_file(tmp_path)
    ms = mediadl.load_settings()
    for p in ms["presets"]:
        p.update(cookies_source="browser", cookies_browser="brave")
    next(p for p in ms["presets"] if p["id"] == "bimusic").update(cookies_browser="firefox")
    mediadl.save_settings(ms)
    assert svc().cookies_import(str(src))["ok"]
    by_id = {p["id"]: p for p in mediadl.load_settings()["presets"]}
    assert by_id["biliked"]["cookies_source"] == "vault"
    assert by_id["bimusic"]["cookies_source"] == "browser"  # Firefox works: left alone
