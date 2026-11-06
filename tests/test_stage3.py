"""Analyze before downloading, clips, chapters, subtitles."""

import json
import subprocess

import pytest

from bakin import mediadl, tools


def preset(**kw):
    return mediadl.clean_preset(mediadl.new_preset("P", **kw))


def arg(args, flag):
    return args[args.index(flag) + 1]


@pytest.mark.parametrize("text,expected", [
    ("1:02:10-1:15:00", "3730-4500"), ("62:10 - 75:00", "3730-4500"), ("1h2m10s-1h15m", "3730-4500"),
    ("90-", "90-"), ("-30", "0-30"), ("5:00-4:00", ""), ("abc-def", ""), ("", ""), ("120", ""),
])
def test_parse_section(text, expected):
    assert mediadl.parse_section(text) == expected


def test_clip_args_skip_archive_and_name_the_part(tmp_path):
    a = mediadl.build_args(preset(mode="video", section="1:02:10-1:15:00"), "https://youtu.be/x", tmp_path, with_cookies=False)
    assert arg(a, "--download-sections") == "*3730-4500" and "--force-keyframes-at-cuts" in a
    assert "--download-archive" not in a
    assert "(tramo 1h02m10s-1h15m00s).%(ext)s" in arg(a, "--output")
    b = mediadl.build_args(preset(mode="audio", section="90-"), "https://youtu.be/x", tmp_path, with_cookies=False)
    assert arg(b, "--download-sections") == "*90-inf" and "--force-keyframes-at-cuts" not in b


def test_chapters_and_subtitles_args(tmp_path):
    a = mediadl.build_args(preset(split_chapters=True), "https://youtu.be/x", tmp_path, with_cookies=False)
    i = a.index("--split-chapters")
    assert a[i + 1] == "-o" and a[i + 2].startswith("chapter:") and "%(section_number)03d" in a[i + 2]
    v = mediadl.build_args(preset(mode="video", subtitles="es.*, en, ; $(x) --exec"), "https://youtu.be/x", tmp_path, with_cookies=False)
    assert arg(v, "--sub-langs") == "es.*,en" and "--embed-subs" in v
    au = mediadl.build_args(preset(mode="audio", subtitles="es.*"), "https://youtu.be/x", tmp_path, with_cookies=False)
    assert "--write-subs" not in au


class FakeProc:
    def __init__(self, code, out, err=b""):
        self.returncode, self._out, self._err, self.pid = code, out, err, 1

    def communicate(self, timeout=None):
        return self._out, self._err


def test_analyze_video_and_playlist(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "find", lambda name, exe=None: tmp_path / "yt-dlp.exe" if name == "yt-dlp" else None)
    video = {"id": "abc", "title": "Tema", "duration": 200, "uploader": "Artista", "thumbnail": "https://i/x.jpg",
             "formats": [{"vcodec": "avc1", "acodec": "none", "height": 1080},
                         {"vcodec": "none", "acodec": "opus", "abr": 130.4}, {"vcodec": "none", "acodec": "mp4a.40.2", "abr": 128}],
             "chapters": [{"title": "Intro", "start_time": 0, "end_time": 30}], "subtitles": {"es": [], "en": []},
             "automatic_captions": {"fr": []}}
    playlist = {"title": "Lista", "entries": [{"id": "a1", "title": "Uno", "duration": 100}, {"id": "b2", "title": "Dos"}]}
    calls = []

    def popen(args, **kw):
        calls.append(args)
        return FakeProc(0, json.dumps(playlist if "list=" in args[-1] else video).encode())
    monkeypatch.setattr(subprocess, "Popen", popen)
    v = mediadl.analyze("https://youtu.be/abc", preset())
    assert (v["type"], v["max_height"], v["audio"], v["subtitles"], len(v["chapters"])) == ("video", 1080, "opus 130 kbps", ["en", "es"], 1)
    p = mediadl.analyze("https://youtube.com/playlist?list=PL1", preset())
    assert p["type"] == "playlist" and p["count"] == 2 and p["entries"][1]["index"] == 2
    assert all("--cookies" not in c for c in calls)        # cookies "off": never


def test_analyze_bot_check_is_not_answered_with_the_account(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "find", lambda name, exe=None: tmp_path / "yt-dlp.exe" if name == "yt-dlp" else None)
    f = tmp_path / "cookies.txt"
    f.write_text("# Netscape HTTP Cookie File\n")
    calls = []

    def popen(args, **kw):
        calls.append("--cookies" in args)
        return FakeProc(1, b"", b"ERROR: [youtube] x: Sign in to confirm you're not a bot")
    monkeypatch.setattr(subprocess, "Popen", popen)
    with pytest.raises(mediadl.MediaError, match="frenando"):
        mediadl.analyze("https://youtu.be/x", preset(cookies_mode="auto", cookies_source="file", cookies_file=str(f)))
    assert calls == [False]
