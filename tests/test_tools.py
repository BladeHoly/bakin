"""External tools (bakin.tools): versions, checksums and installing, with a fake GitHub: no network.
Everything lands in the test's BAKIN_HOME."""

import hashlib
import io
import sys
import threading
import zipfile

import pytest

from bakin import tools


@pytest.mark.parametrize("out,want", [
    ("2026.10.22\n", "2026.10.22"),
    ("ffmpeg version N-121000-g1234 Copyright (c) 2000-2026\nbuilt with gcc", "N-121000-g1234"),
    ("deno 2.5.4 (stable, release, x86_64-pc-windows-msvc)\nv8 14.0", "2.5.4"),
    ("", None),
    ("\n\n", None),
])
def test_parse_version(out, want):
    assert tools._parse_version(out) == want


class Resp:
    def __init__(self, body: bytes = b"", status=200):
        self.body, self.status_code = body, status
        self.text = body.decode("utf-8", "replace")
        self.headers = {"Content-Length": str(len(body))}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise tools.requests.HTTPError(str(self.status_code))

    def iter_content(self, size):
        for i in range(0, len(self.body), 4):
            yield self.body[i:i + 4]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeGitHub:
    """A requests.Session that serves a fixed set of URLs."""

    def __init__(self, files: dict):
        self.files, self.headers, self.asked = files, {}, []

    def get(self, url, timeout=None, stream=False):
        self.asked.append(url)
        if url not in self.files:
            return Resp(status=404)
        return Resp(self.files[url])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def github(monkeypatch):
    """Installs the fake in place of requests.Session; returns a setter for its files."""
    holder = {}

    def serve(files):
        holder["gh"] = FakeGitHub(files)
        return holder["gh"]
    monkeypatch.setattr(tools.requests, "Session", lambda: holder["gh"])
    monkeypatch.setattr(tools, "version", lambda name, refresh=False: "1.0")
    return serve


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def zipped(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_expected_sha_picks_the_right_line():
    t = tools.TOOLS["ffmpeg"]
    sums = f"{'a' * 64}  other.zip\n{'B' * 64}  ffmpeg-master-latest-win64-gpl.zip\n".encode()
    assert tools._expected_sha(t, FakeGitHub({t.sums_url: sums})) == "b" * 64
    with pytest.raises(tools.ToolError):  # the release lists other files only
        tools._expected_sha(t, FakeGitHub({t.sums_url: f"{'a' * 64}  other.zip\n".encode()}))
    deno = tools.TOOLS["deno"]  # its sums file holds only the hash
    assert tools._expected_sha(deno, FakeGitHub({deno.sums_url: ("C" * 64).encode()})) == "c" * 64


def test_install_a_single_exe_and_remember_its_hash(github):
    exe = b"MZ fake yt-dlp"
    t = tools.TOOLS["yt-dlp"]
    github({t.sums_url: f"{sha(exe)}  yt-dlp.exe\n".encode(), t.url: exe})
    seen = []
    assert tools.install("yt-dlp", progress=lambda d, total: seen.append((d, total))) == "1.0"
    assert tools.managed_path("yt-dlp").read_bytes() == exe
    assert seen[-1] == (len(exe), len(exe))
    assert tools._state()["yt-dlp"]["sha256"] == sha(exe)
    assert [p.name for p in tools.tools_dir().iterdir() if p.suffix == ".download"] == []
    # the same release again: nothing new; another one: an update
    github({t.sums_url: f"{sha(exe)}  yt-dlp.exe\n".encode()})
    assert tools.update_available("yt-dlp") is False
    github({t.sums_url: f"{'d' * 64}  yt-dlp.exe\n".encode()})
    assert tools.update_available("yt-dlp") is True


def test_install_from_a_zip_replaces_the_old_copy(github):
    t = tools.TOOLS["ffmpeg"]
    old = tools.managed_path("ffmpeg")
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old")
    z = zipped({"ffmpeg-x/bin/ffmpeg.exe": b"new ffmpeg", "ffmpeg-x/bin/ffprobe.exe": b"new ffprobe"})
    github({t.sums_url: f"{sha(z)}  ffmpeg-master-latest-win64-gpl.zip\n".encode(), t.url: z})
    tools.install("ffmpeg")
    assert old.read_bytes() == b"new ffmpeg"
    assert tools.managed_path("ffmpeg", "ffprobe.exe").read_bytes() == b"new ffprobe"
    assert not old.parent.with_name("ffmpeg.old").exists()


def test_install_refuses_a_file_that_does_not_match(github):
    t = tools.TOOLS["yt-dlp"]
    github({t.sums_url: f"{'e' * 64}  yt-dlp.exe\n".encode(), t.url: b"tampered"})
    with pytest.raises(tools.ToolError, match="checksum"):
        tools.install("yt-dlp")
    assert not tools.managed_path("yt-dlp").exists()
    assert [p for p in tools.tools_dir().iterdir() if p.is_file() and p.suffix == ".download"] == []


def test_install_a_zip_without_the_exe_keeps_the_old_copy(github):
    t = tools.TOOLS["deno"]
    old = tools.managed_path("deno")
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old deno")
    z = zipped({"README.txt": b"nothing here"})
    github({t.sums_url: sha(z).encode(), t.url: z})
    with pytest.raises(tools.ToolError, match="deno.exe"):
        tools.install("deno")
    assert old.read_bytes() == b"old deno"


def test_install_can_be_cancelled(github):
    exe = b"x" * 64
    t = tools.TOOLS["yt-dlp"]
    github({t.sums_url: f"{sha(exe)}  yt-dlp.exe\n".encode(), t.url: exe})
    stop = threading.Event()
    stop.set()
    with pytest.raises(tools.ToolError, match="cancelado"):
        tools.install("yt-dlp", cancel=stop)
    assert not tools.managed_path("yt-dlp").exists()


def test_find_prefers_bakins_copy_then_path(monkeypatch):
    monkeypatch.setattr(tools.shutil, "which", lambda exe: None)
    assert tools.find("deno") is None and tools.missing(("deno",)) == ["deno"]
    assert tools.update_available("deno") is None  # not Bakin's to update
    monkeypatch.setattr(tools.shutil, "which", lambda exe: rf"C:\bin\{exe}")
    assert tools.find("deno").name == "deno.exe" and tools.missing(("deno",)) == []
    mine = tools.managed_path("deno")
    mine.parent.mkdir(parents=True)
    mine.write_bytes(b"x")
    assert tools.find("deno") == mine


def test_version_is_cached_until_the_file_changes(monkeypatch):
    p = tools.managed_path("yt-dlp")
    p.parent.mkdir(parents=True)
    p.write_bytes(b"x")
    runs = []
    monkeypatch.setattr(tools, "run_quick", lambda cmd, timeout=20: runs.append(cmd) or "2026.10.22\n")
    assert tools.version("yt-dlp") == "2026.10.22"
    assert tools.version("yt-dlp") == "2026.10.22" and len(runs) == 1
    assert tools.version("yt-dlp", refresh=True) and len(runs) == 2
    monkeypatch.setattr(tools, "run_quick", lambda cmd, timeout=20: None)  # it didn't start
    assert tools.version("yt-dlp", refresh=True) is None


def test_status_and_mark_checked(monkeypatch):
    monkeypatch.setattr(tools.shutil, "which", lambda exe: None)
    tools.mark_checked("yt-dlp")
    st = tools.status()
    assert set(st) == {"yt-dlp", "ffmpeg", "deno"}
    assert st["yt-dlp"]["installed"] is False and st["yt-dlp"]["checked_at"]
    assert st["ffmpeg"]["size"] == "200 MB" and st["ffmpeg"]["checked_at"] is None


def test_a_broken_state_file_is_forgotten():
    tools.tools_dir().mkdir(parents=True)
    tools._state_path().write_text("{not json", encoding="utf-8")
    assert tools._state() == {}


def test_run_quick():
    assert tools.run_quick([sys.executable, "-c", "print('hola')"]).strip() == "hola"
    assert tools.run_quick([str(tools.tools_dir() / "no-such.exe")]) is None
