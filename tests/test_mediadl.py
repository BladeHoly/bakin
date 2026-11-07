"""Music/video downloads (bakin.mediadl): arguments, presets, archive, parsing.
No network: yt-dlp is never run here (see the live check in docs/VALIDATION.md)."""

import json
import threading
from pathlib import Path

import pytest

from bakin import mediadl, tools


def preset(**kw):
    return mediadl.clean_preset(mediadl.new_preset("P", **kw))


def arg(args, flag):
    return args[args.index(flag) + 1]


def test_audio_args_match_the_script(tmp_path):
    a = mediadl.build_args(preset(audio_format="flac", sponsorblock="sponsor, selfpromo",
                                  rate_limit="2M", playlist_items="1:10"),
                           "https://youtu.be/x", tmp_path, with_cookies=False)
    assert ["--extract-audio", "--audio-format", "flac", "--audio-quality", "0"] == a[a.index("--extract-audio"):a.index("--extract-audio") + 5]
    for flag in ("--embed-thumbnail", "--embed-metadata", "--no-overwrites", "--no-abort-on-error", "--ignore-errors"):
        assert flag in a
    assert arg(a, "--download-archive") == str(tmp_path / "archive.txt")
    assert arg(a, "--sponsorblock-remove") == "sponsor,selfpromo"
    assert arg(a, "--limit-rate") == "2M" and arg(a, "--playlist-items") == "1:10"
    assert (arg(a, "--sleep-interval"), arg(a, "--max-sleep-interval")) == ("4", "9")
    assert a[-2:] == ["--", "https://youtu.be/x"]          # a URL can never be read as an option
    assert "--cookies" not in a and "--cookies-from-browser" not in a


def test_video_args_with_height_cap(tmp_path):
    a = mediadl.build_args(preset(mode="video", video_max_height=1080), "https://x.test/v", tmp_path, with_cookies=False)
    assert "[height<=1080]" in arg(a, "--format") and arg(a, "--merge-output-format") == "mp4"
    assert "--extract-audio" not in a


def test_cookies_from_brave_profile_or_file(tmp_path):
    a = mediadl.build_args(preset(cookies_browser="brave", cookies_profile="Profile 1"), "https://x", tmp_path, with_cookies=True)
    assert arg(a, "--cookies-from-browser") == "brave:Profile 1"
    f = tmp_path / "cookies.txt"
    f.write_text("# Netscape HTTP Cookie File\n")
    a = mediadl.build_args(preset(cookies_source="file", cookies_file=str(f)), "https://x", tmp_path, with_cookies=True)
    assert arg(a, "--cookies") == str(f)
    with pytest.raises(mediadl.MediaError):
        mediadl.build_args(preset(cookies_source="file", cookies_file=str(tmp_path / "nope.txt")), "https://x", tmp_path, with_cookies=True)


def test_clean_preset_rejects_junk():
    p = mediadl.clean_preset({"name": "x", "audio_format": "exe", "audio_quality": "999", "mode": "rm -rf",
                              "cookies_browser": "netscape", "cookies_profile": "a:b\nc", "rate_limit": "fast; del",
                              "playlist_items": "1;calc", "sleep_min": 50, "sleep_max": 3, "template": "%(title)s",
                              "sponsorblock": "sponsor;rm"})
    assert (p["audio_format"], p["audio_quality"], p["mode"], p["cookies_browser"]) == ("mp3", "0", "audio", "brave")
    assert p["cookies_profile"] == "abc" and p["rate_limit"] == "" and p["playlist_items"] == ""
    assert p["sleep_max"] == 50 and p["template"].endswith(".%(ext)s") and p["sponsorblock"] == ""


def test_template_caps_keep_id_and_ext():
    short = mediadl.cap_template(mediadl.TEMPLATES["artist"], Path("C:/Music"))
    assert "%(title).140s" in short and "%(uploader).50s" in short and "[%(id)s].%(ext)s" in short
    deep = mediadl.cap_template(mediadl.TEMPLATES["artist"], Path("C:/" + "x" * 150))
    assert "%(title).40s" in deep
    custom = mediadl.cap_template("%(title).20s.%(ext)s", Path("C:/M"))
    assert custom == "%(title).20s.%(ext)s"


def test_import_presets_from_the_script(tmp_path):
    f = tmp_path / "yt-dlp-presets.json"
    f.write_text(json.dumps({
        "Rap": {"Description": "temas", "Config": {
            "OutputDir": "Hip-Hop", "Format": "m4a", "Quality": "320", "Cookies": "brave",
            "SponsorBlock": True, "OutputTemplate": "%(title)s [%(id)s].%(ext)s",
            "Delays": {"Min": 2, "Max": 5, "Playlist": 30},
            "ExtraOpts": {"DownloadVideo": False, "RateLimit": "1M", "PlaylistItems": ""}}},
        "VODs": {"Description": "", "Config": {"OutputDir": "Vods", "Format": "mp3", "Cookies": "file:C:\\c.txt",
                                               "ExtraOpts": {"DownloadVideo": True}}},
    }), encoding="utf-8-sig")
    rap, vods = mediadl.import_script_presets(f)
    assert (rap["name"], rap["folder"], rap["audio_format"], rap["audio_quality"]) == ("Rap", "Hip-Hop", "m4a", "320")
    assert (rap["cookies_mode"], rap["cookies_browser"]) == ("always", "brave")
    assert rap["sponsorblock"] == "sponsor,selfpromo,intro,outro" and rap["playlist_delay"] == 30
    assert vods["mode"] == "video" and vods["cookies_source"] == "file" and vods["cookies_file"] == "C:\\c.txt"


def test_scan_existing_registers_ids_once(tmp_path, monkeypatch):
    monkeypatch.setattr(mediadl, "_id_from_tags", lambda p: "AAAAAAAAAAA" if p.name == "tagged.mp3" else None)
    (tmp_path / "Artist - Song [dQw4w9WgXcQ].mp3").write_bytes(b"x")
    (tmp_path / "tagged.mp3").write_bytes(b"x")
    (tmp_path / "no id.flac").write_bytes(b"x")
    (tmp_path / "cover.jpg").write_bytes(b"x")
    (tmp_path / "archive.txt").write_text("youtube AAAAAAAAAAA\n", encoding="utf-8")
    r = mediadl.scan_existing(tmp_path)
    assert (r["files"], r["registered"], r["already"], r["no_id"]) == (3, 1, 1, ["no id.flac"])
    assert (tmp_path / "archive.txt").read_text().splitlines() == ["youtube AAAAAAAAAAA", "youtube dQw4w9WgXcQ"]
    assert mediadl.scan_existing(tmp_path)["registered"] == 0


def test_leftovers_and_forgetting_them(tmp_path):
    (tmp_path / "Song [abcdefghijk].mp4").write_bytes(b"x")
    (tmp_path / "Song [zzzzzzzzzzz].mp3").write_bytes(b"x")
    (tmp_path / "archive.txt").write_text("youtube abcdefghijk\nyoutube zzzzzzzzzzz\n", encoding="utf-8")
    (left,) = mediadl.leftover_videos(tmp_path)
    assert left["id"] == "abcdefghijk"
    assert mediadl.forget_in_archive(tmp_path, ["abcdefghijk"]) == 1
    assert (tmp_path / "archive.txt").read_text() == "youtube zzzzzzzzzzz\n"


def test_runner_turns_lines_into_events():
    events = []
    r = mediadl.Runner(events.append, threading.Event())
    seen = {"done": [], "errors": [], "started": [], "n_entries": None}
    for line in [
        '[bakin-s]{"id": "a1", "title": "Tema", "n_entries": 3, "playlist_index": 1}',
        '[bakin-p]{"status": "downloading", "downloaded_bytes": 10, "total_bytes": 100, "speed": 5, "eta": 18}',
        '[bakin-pp]{"status": "started", "postprocessor": "ExtractAudio"}',
        '[bakin-d]{"id": "a1", "title": "Tema", "filepath": "C:\\\\M\\\\Tema [a1].mp3"}',
        "ERROR: [youtube] b2: Sign in to confirm your age. This video may be inappropriate for some users.",
        "ERROR: Could not copy Chrome cookie database. See https://github.com/yt-dlp/yt-dlp/issues/7271",
        "[youtube] a1: Downloading webpage",
    ]:
        r._line(line, seen)
    kinds = [e["kind"] for e in events]
    assert kinds == ["media_item", "media_progress", "media_post", "media_file", "error", "error"]
    assert events[1]["total"] == 100 and events[3]["path"].endswith("Tema [a1].mp3")
    assert "iniciar sesión" in events[4]["message"] and "ciérralo" in events[5]["message"]
    assert seen["n_entries"] == 3 and len(seen["errors"]) == 2


def test_auto_cookies_retry_only_when_the_site_asks(tmp_path, monkeypatch):
    calls = []

    def fake_run(self, args):
        with_cookies = "--cookies-from-browser" in args
        calls.append(with_cookies)
        if with_cookies:
            return 0, {"done": [{"id": "v", "filepath": ""}], "errors": [], "started": ["v"], "n_entries": None}
        return 1, {"done": [], "errors": ["[youtube] v: Sign in to confirm your age"], "started": ["v"], "n_entries": None}

    monkeypatch.setattr(mediadl.Runner, "run", fake_run)
    monkeypatch.setattr(tools, "find", lambda *a, **k: None)
    events = []
    stats = mediadl.download(["https://youtu.be/v"], preset(cookies_mode="auto"), tmp_path, events.append, threading.Event())
    assert calls == [False, True] and stats.done == 1 and stats.failed == 0
    calls.clear()
    mediadl.download(["https://youtu.be/v"], preset(cookies_mode="off"), tmp_path, events.append, threading.Event())
    assert calls == [False]                                  # never uses cookies when off


def test_media_folder_is_a_library_collection(tmp_path):
    from bakin import library
    p = preset(folder="Música")
    mediadl.mark_folder(mediadl.destination(p, tmp_path), p)
    (tmp_path / "Música" / "Tema [abcdefghijk].mp3").write_bytes(b"x")
    (tmp_path / "Música" / "Tema [abcdefghijk].webm.part").write_bytes(b"x")
    (tmp_path / "Música" / "archive.txt").write_text("youtube abcdefghijk\n")
    (col,) = library.collections(tmp_path)
    assert (col.id, col.kind) == ("md:Música", "media")
    assert [i["name"] for i in library.scan(col)] == ["Tema [abcdefghijk].mp3"]


def test_tools_checksum_lookup():
    class R:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            pass

    class S:
        def __init__(self, text):
            self.t = text

        def get(self, *a, **k):
            return R(self.t)

    sums = "aa" * 32 + "  yt-dlp_linux\n" + "bb" * 32 + "  yt-dlp.exe\n"
    assert tools._expected_sha(tools.TOOLS["yt-dlp"], S(sums)) == "bb" * 32
    ps = "\nAlgorithm : SHA256\nHash      : " + "CC" * 32 + "\n"
    assert tools._expected_sha(tools.TOOLS["deno"], S(ps)) == "cc" * 32
    with pytest.raises(tools.ToolError):
        tools._expected_sha(tools.TOOLS["yt-dlp"], S("nothing here"))


def test_run_quick_never_hangs_and_kills_the_whole_tree():
    import time
    t = time.time()
    # cmd starts a child (ping) that would keep the pipe open after cmd dies
    assert tools.run_quick(["cmd", "/c", "ping -n 30 127.0.0.1 >nul & echo late"], timeout=1) is None
    assert time.time() - t < 10
    assert "hola" in tools.run_quick(["cmd", "/c", "echo hola"])


def test_tool_versions_are_cached(monkeypatch, tmp_path):
    exe = tmp_path / "yt-dlp.exe"
    exe.write_bytes(b"x")
    monkeypatch.setattr(tools, "find", lambda name, e=None: exe)
    calls = []
    monkeypatch.setattr(tools, "run_quick", lambda cmd, timeout=20: calls.append(cmd) or "2026.10.26\n")
    assert tools.version("yt-dlp") == "2026.10.26"
    assert tools.version("yt-dlp") == "2026.10.26" and len(calls) == 1
    assert tools.version("yt-dlp", refresh=True) and len(calls) == 2


def test_tools_get_a_clean_pyinstaller_environment():
    assert tools.child_env()["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


def test_locked_browser_cookies_fall_back_to_no_cookies_without_retry_storm(tmp_path, monkeypatch):
    calls = []

    def fake_run(self, args):
        with_cookies = "--cookies-from-browser" in args
        calls.append(with_cookies)
        if with_cookies:
            return 1, {"done": [], "errors": ["Could not copy Chrome cookie database. See ..."], "started": [], "n_entries": None}
        return 0, {"done": [{"id": "v", "filepath": ""}], "errors": [], "started": ["v"], "n_entries": None}

    monkeypatch.setattr(mediadl.Runner, "run", fake_run)
    monkeypatch.setattr(tools, "find", lambda *a, **k: None)
    events = []
    stats = mediadl.download(["https://youtu.be/v"], preset(cookies_mode="always"), tmp_path, events.append,
                             threading.Event(), retry_on_partial=True)
    assert calls == [True, False] and stats.done == 1
    assert any("sin cookies" in e.get("message", "") for e in events)


def test_builtin_presets_fresh_install():
    s = mediadl.load_settings()
    ids = [p["id"] for p in s["presets"]]
    assert ids == ["bimusic", "biliked", "biplaylist", "bimp3", "bivideo", "bishare"]
    assert s["default_preset"] == "bimusic"
    liked = next(p for p in s["presets"] if p["id"] == "biliked")
    music = next(p for p in s["presets"] if p["id"] == "bimusic")
    assert liked["cookies_mode"] == "always" and music["cookies_mode"] == "auto"
    assert liked["folder"] == music["folder"] and liked["archive"] and liked["audio_format"] == "opus"
    assert all(p["builtin"] for p in s["presets"])
    assert mediadl.load_settings()["presets"] == s["presets"]          # stable, saved once


def test_builtins_join_existing_installs_once_with_the_users_music_folder(tmp_path):
    mine = mediadl.clean_preset(mediadl.new_preset("Música MP3", id="abc123", audio_format="opus",
                                                   folder=r"R:\Cosas\Music", cookies_mode="always"))
    mediadl.save_settings({"presets": [mine], "default_preset": "abc123"})
    s = mediadl.load_settings()
    assert [p["id"] for p in s["presets"]][0] == "abc123" and s["default_preset"] == "abc123"   # theirs untouched
    by = {p["id"]: p for p in s["presets"]}
    assert by["bimusic"]["folder"] == by["biliked"]["folder"] == r"R:\Cosas\Music"
    assert by["bivideo"]["folder"] == "Videos"
    # deleting a built-in sticks; edits stick; restore brings back only what is missing
    mediadl.save_settings({**s, "presets": [p for p in s["presets"] if p["id"] != "bimp3"]})
    edited = mediadl.load_settings()
    edited["presets"][1]["name"] = "Mi música"
    mediadl.save_settings(edited)
    again = mediadl.load_settings()
    assert "bimp3" not in {p["id"] for p in again["presets"]}
    assert mediadl.restore_builtins() == 1
    final = {p["id"]: p for p in mediadl.load_settings()["presets"]}
    assert "bimp3" in final and final["bimusic"]["name"] == "Mi música"


def test_playlist_preset_numbers_and_groups_by_playlist(tmp_path):
    p = next(x for x in mediadl.builtin_presets() if x["id"] == "biplaylist")
    a = mediadl.build_args(mediadl.clean_preset(p), "https://youtube.com/playlist?list=X", tmp_path, with_cookies=False)
    out = arg(a, "--output")
    assert "%(playlist_title)s" in out and "%(playlist_index)03d" in out and "[%(id)s].%(ext)s" in out


def test_youtube_blocking_stops_the_run(tmp_path, monkeypatch):
    calls = []

    def fake_run(self, args):
        calls.append(args[-1])
        return 1, {"done": [], "errors": ["[youtube] x: Sign in to confirm you're not a bot"], "started": [], "n_entries": None}

    monkeypatch.setattr(mediadl.Runner, "run", fake_run)
    monkeypatch.setattr(tools, "find", lambda *a, **k: None)
    stats = mediadl.download(["https://youtu.be/a", "https://youtu.be/b"], preset(cookies_mode="off", playlist_delay=0),
                             tmp_path, lambda e: None, threading.Event())
    assert stats.blocked and calls == ["https://youtu.be/a"]


def test_youtube_opus_is_not_low_quality():
    from bakin import audiotags
    assert not audiotags.is_low_quality({"codec": "opus", "bitrate": 128, "lossless": False})
    assert audiotags.is_low_quality({"codec": "mp3", "bitrate": 320, "lossless": False})
    assert audiotags.is_low_quality({"codec": "aac", "bitrate": 96, "lossless": False})


def test_app_bound_cookie_error_explains_cookies_txt():
    msg = mediadl.friendly_error("Failed to decrypt with DPAPI. See https://github.com/yt-dlp/yt-dlp/issues/10927")
    assert "cookies.txt" in msg and "Firefox" in msg


def test_bot_check_neither_retries_nor_spends_the_account(tmp_path, monkeypatch):
    calls = []

    def fake_run(self, args):
        calls.append("--cookies" in args or "--cookies-from-browser" in args)
        return 1, {"done": [], "errors": ["[youtube] x: Sign in to confirm you're not a bot. Use --cookies"],
                   "started": ["x"], "n_entries": None}

    monkeypatch.setattr(mediadl.Runner, "run", fake_run)
    monkeypatch.setattr(tools, "find", lambda *a, **k: None)
    f = tmp_path / "cookies.txt"
    f.write_text("# Netscape HTTP Cookie File\n")
    stats = mediadl.download(["https://youtu.be/x", "https://youtu.be/y"],
                             preset(cookies_mode="auto", cookies_source="file", cookies_file=str(f)),
                             tmp_path, lambda e: None, threading.Event(), retry_on_partial=True)
    assert calls == [False] and stats.blocked        # one try, no cookies, stop


def test_age_gate_still_uses_cookies_in_auto_mode(tmp_path, monkeypatch):
    calls = []

    def fake_run(self, args):
        with_cookies = "--cookies" in args
        calls.append(with_cookies)
        if with_cookies:
            return 0, {"done": [{"id": "x", "filepath": ""}], "errors": [], "started": ["x"], "n_entries": None}
        return 1, {"done": [], "errors": ["[youtube] x: Sign in to confirm your age. This video may be inappropriate"],
                   "started": ["x"], "n_entries": None}

    monkeypatch.setattr(mediadl.Runner, "run", fake_run)
    monkeypatch.setattr(tools, "find", lambda *a, **k: None)
    f = tmp_path / "cookies.txt"
    f.write_text("# Netscape HTTP Cookie File\n")
    stats = mediadl.download(["https://youtu.be/x"], preset(cookies_mode="auto", cookies_source="file", cookies_file=str(f)),
                             tmp_path, lambda e: None, threading.Event())
    assert calls[-1] is True and stats.done == 1 and not stats.blocked


# ------------------------------------------------------------- audio or video

V = [{"vcodec": "avc1", "acodec": "none", "height": 1080}, {"vcodec": "none", "acodec": "opus", "abr": 130}]
A = [{"vcodec": "none", "acodec": "opus", "abr": 160}]


@pytest.mark.parametrize("info,url,kind", [
    ({"extractor_key": "Soundcloud", "formats": A}, "https://soundcloud.com/a/b", "audio"),
    ({"extractor_key": "Bandcamp", "formats": A}, "https://artist.bandcamp.com/track/x", "audio"),
    ({"extractor_key": "Youtube", "formats": V}, "https://music.youtube.com/watch?v=abcdefghijk", "audio"),
    ({"extractor_key": "Youtube", "formats": V, "webpage_url": "https://www.youtube.com/watch?v=abcdefghijk"},
     "https://music.youtube.com/watch?v=abcdefghijk", "audio"),
    ({"extractor_key": "Youtube", "formats": V, "track": "Song", "artist": "Band"}, "https://www.youtube.com/watch?v=abcdefghijk", "audio"),
    ({"extractor_key": "Youtube", "formats": V, "categories": ["Music"]}, "https://youtu.be/abcdefghijk", "audio"),
    ({"extractor_key": "Generic", "formats": A}, "https://example.com/podcast.mp3", "audio"),
    ({"extractor_key": "Youtube", "formats": V, "categories": ["Gaming"]}, "https://www.youtube.com/watch?v=abcdefghijk", "video"),
    ({"extractor_key": "Twitch", "formats": V}, "https://www.twitch.tv/videos/1", "video"),
    ({"extractor_key": "YoutubeTab", "entries": [{"ie_key": "Youtube"}]}, "https://www.youtube.com/playlist?list=PLx", "unknown"),
    ({"extractor_key": "SoundcloudSet", "entries": [{"ie_key": "Soundcloud"}]}, "https://soundcloud.com/a/sets/b", "audio"),
])
def test_classify_music_or_video(info, url, kind):
    assert mediadl.classify_media(info, url)["kind"] == kind


def test_site_of():
    assert mediadl.site_of("https://www.youtube.com/watch?v=x") == "youtube.com"
    assert mediadl.site_of("https://m.youtube.com/watch?v=x") == "youtube.com"
    assert mediadl.site_of("https://music.youtube.com/watch?v=x") == "music.youtube.com"
    assert mediadl.site_of("nope") == ""


def test_suggested_preset_follows_what_was_detected_then_the_users_choice():
    s = mediadl.load_settings()
    video = mediadl.suggest_preset(s, "https://www.youtube.com/watch?v=x", {"kind": "video", "why": "tiene video"})
    music = mediadl.suggest_preset(s, "https://soundcloud.com/a/b", {"kind": "audio", "why": "x"})
    mode = {p["id"]: p["mode"] for p in s["presets"]}
    assert mode[video["preset"]] == "video"
    assert mode[music["preset"]] == "audio"
    assert mediadl.suggest_preset(s, "https://www.youtube.com/playlist?list=PL", {"kind": "unknown"})["preset"] is None
    s["site_prefs"] = {"youtube.com": music["preset"]}
    again = mediadl.suggest_preset(s, "https://www.youtube.com/watch?v=y", {"kind": "video"})
    assert again["preset"] == music["preset"] and "youtube.com" in again["why"]


def test_friendly_errors_for_disk_network_and_gone():
    # PLAN 0.23.4: the three things that go wrong most, in plain words (the raw text stays after)
    full = mediadl.friendly_error("ERROR: unable to write data: [Errno 28] No space left on device")
    assert full.startswith("No queda espacio en el disco") and "Errno 28" in full
    win_full = mediadl.friendly_error("OSError: [WinError 112] There is not enough space on the disk")
    assert win_full.startswith("No queda espacio")
    offline = mediadl.friendly_error("ERROR: Unable to download webpage: <urlopen error [Errno 11001] getaddrinfo failed>")
    assert offline.startswith("No hay conexión a internet")
    gone = mediadl.friendly_error("ERROR: [youtube] abc: Private video. Sign in if you've been granted access")
    assert gone.startswith("El video es privado")
    assert mediadl.friendly_error("ERROR: [youtube] abc: Video unavailable").startswith("El video es privado")
    assert mediadl.friendly_error("something else") == "something else"


def test_spotify_links_never_go_to_ytdlp_and_drm_is_explained():
    # a friend (2026-11-07): a Spotify playlist ended in «[DRM] … will NOT be supported»
    from bakin import service
    for url in ("https://open.spotify.com/playlist/4QcQTjpSGkoznLFbreOfWI?si=x", "spotify:track:abc"):
        assert mediadl.is_spotify(url)
        r = service.Service(lambda e: None).start("download", url)
        assert not r["ok"] and r.get("spotify") and "YouTube Music" in r["error"]
    assert not mediadl.is_spotify("https://www.youtube.com/watch?v=x")
    msg = mediadl.friendly_error("ERROR: [DRM] The requested site is known to use DRM protection. It will NOT be supported.")
    assert "DRM" in msg and "YouTube Music" in msg and "NOT be supported" not in msg
