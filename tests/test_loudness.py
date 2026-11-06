"""Loudness measurement and «Volumen parejo» gains (PLAN 0.16.1)."""

import json
import math
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from bakin import loudness, tools

SONG = """\
Input #0, mp3, from 'song.mp3':
  Duration: 00:03:21.45, start: 0.025057, bitrate: 320 kb/s
  Stream #0:0: Audio: mp3 (mp3float), 44100 Hz, stereo, fltp, 320 kb/s
Stream mapping:
  Stream #0:0 -> #0:0 (mp3 (mp3float) -> pcm_s16le (native))
Output #0, null, to 'pipe:':
  Stream #0:0: Audio: pcm_s16le, 44100 Hz, stereo, s16, 1411 kb/s
[Parsed_ebur128_0 @ 000001ac9dc48300] Summary:

  Integrated loudness:
    I:         -14.2 LUFS
    Threshold: -24.4 LUFS

  Loudness range:
    LRA:         6.3 LU
    Threshold: -34.5 LUFS
    LRA low:   -18.9 LUFS
    LRA high:  -12.6 LUFS

  True peak:
    Peak:       -0.3 dBFS
[out#0/null @ 000001ac9db82b80] video:0KiB audio:34675KiB subtitle:0KiB other streams:0KiB global headers:0KiB muxing overhead: unknown
size=N/A time=00:03:21.42 bitrate=N/A speed= 412x elapsed=0:00:00.49
"""

LOUD = """\
[mp3float @ 0000020a] Header missing
[Parsed_ebur128_0 @ 0000020b5c1a7e40] Summary:

  Integrated loudness:
    I:          -6.8 LUFS
    Threshold: -16.9 LUFS

  Loudness range:
    LRA:         2.1 LU
    Threshold: -26.9 LUFS
    LRA low:    -8.0 LUFS
    LRA high:   -5.9 LUFS

  True peak:
    Peak:        1.4 dBFS
"""

SILENT = """\
[Parsed_ebur128_0 @ 00000281b0534f00] Summary:

  Integrated loudness:
    I:         -70.0 LUFS
    Threshold:   0.0 LUFS

  Loudness range:
    LRA:         0.0 LU
    Threshold:   0.0 LUFS
    LRA low:     0.0 LUFS
    LRA high:    0.0 LUFS

  True peak:
    Peak:       -inf dBFS
"""


def test_parse_summary():
    assert loudness.parse(SONG) == {"lufs": -14.2, "peak_db": -0.3, "lra": 6.3}
    assert loudness.parse(LOUD) == {"lufs": -6.8, "peak_db": 1.4, "lra": 2.1}
    assert loudness.parse(SILENT) == {"lufs": -70.0, "peak_db": loudness.MIN_PEAK, "lra": 0.0}
    json.dumps(loudness.parse(SILENT), allow_nan=False)      # storable


def test_parse_without_summary():
    assert loudness.parse("") is None
    assert loudness.parse("song.mp3: Invalid data found when processing input\n") is None
    assert loudness.parse(SONG.split("  True peak:")[0]) is None


def test_gain_target_preamp_and_peak_limit():
    assert loudness.gain_db({"lufs": -14.2, "peak_db": -6.0}) == -3.8
    assert loudness.gain_db({"lufs": -14.2, "peak_db": -6.0}, preamp=2.0) == -1.8
    assert loudness.gain_db({"lufs": -14.2, "peak_db": -6.0}, target=-14.0) == 0.2
    # a quiet song would need +8 dB, but its peak only leaves room for +3
    assert loudness.gain_db({"lufs": -26.0, "peak_db": -4.0}) == 3.0
    # already over full scale: turned down at least to -1 dBFS
    assert loudness.gain_db({"lufs": -6.8, "peak_db": 1.4}) == -11.2
    assert loudness.gain_db({"lufs": -17.0, "peak_db": 0.5}) == -1.5
    assert loudness.gain_db({}) == 0.0
    assert loudness.gain_db({"peak_db": -3}) == 0.0


def test_gain_album_mode():
    e = {"lufs": -10.0, "peak_db": -20.0}
    assert loudness.gain_db(e, "album", album_lufs=-12.0) == -6.0
    assert loudness.gain_db(e, "album") == -8.0            # no album loudness: track
    assert loudness.gain_db(e, "track", album_lufs=-12.0) == -8.0


def test_album_loudness_energy_average_ignores_silence():
    assert loudness.album_loudness([{"lufs": -10.0}, {"lufs": -10.0}]) == pytest.approx(-10.0)
    expected = 10 * math.log10((10 ** -1 + 10 ** -2) / 2)
    got = loudness.album_loudness([{"lufs": -10.0}, {"lufs": -20.0}, {"lufs": -70.0}, {}])
    assert got == pytest.approx(expected)
    assert got > -15.0                     # louder than the plain mean
    assert loudness.album_loudness([{"lufs": -70.0}]) is None
    assert loudness.album_loudness([]) is None


def test_gains_for_track_and_album():
    data = {"a": {"lufs": -10.0, "peak_db": -20.0}, "b": {"lufs": -20.0, "peak_db": -20.0},
            "c": {"lufs": -14.0, "peak_db": -20.0}, "d": {"lufs": -12.0, "peak_db": -20.0}}
    items = [{"id": "a", "album": "Disco", "artist": "Ana feat. Beto"},
             {"id": "b", "album": "DISCO", "artist": "ana"},
             {"id": "c", "album": "", "artist": "Ana"},
             {"id": "d", "album": "Disco", "artist": "Otro"},
             {"id": "e", "album": "Disco", "artist": "Ana"}]          # not measured
    track = loudness.gains_for(items, "track", 0.0, data)
    assert track == {"a": -8.0, "b": 2.0, "c": -4.0, "d": -6.0}
    album = loudness.gains_for(items, "album", 1.0, data)
    alb = loudness.album_loudness([data["a"], data["b"]])
    assert album["a"] == album["b"] == round(-18.0 - alb + 1.0, 2)
    assert album["c"] == -3.0                  # no album: track mode
    assert album["d"] == -5.0                  # another artist: its own album
    assert "e" not in album


def test_gains_for_reads_saved_file():
    loudness._save({"a": {"lufs": -18.0, "peak_db": -5.0, "size": 1, "mtime": 1}})
    assert loudness.gains_for([{"id": "a"}], "track", 0.5) == {"a": 0.5}


def _items(n):
    return [{"id": f"id{i}", "path": f"C:/m/{i}.mp3", "size": 100 + i, "mtime": 1000 + i,
             "album": "", "artist": ""} for i in range(n)]


def test_scan_skips_unchanged_and_remeasures_changed(monkeypatch):
    calls = []

    def fake(path, cancel=None):
        calls.append(Path(path).name)
        return {"lufs": -12.345, "peak_db": -1.0}

    monkeypatch.setattr(loudness, "measure", fake)
    items = _items(3)
    progress = []
    r = loudness.scan(items, threading.Event(), lambda d, t: progress.append((d, t)))
    assert r == {"measured": 3, "skipped": 0, "failed": 0}
    assert progress[0] == (0, 3) and progress[-1] == (3, 3)
    data = loudness.load()
    assert data["id0"] == {"lufs": -12.35, "peak_db": -1.0, "size": 100, "mtime": 1000}

    calls.clear()
    assert loudness.scan(items, threading.Event()) == {"measured": 0, "skipped": 3, "failed": 0}
    assert calls == []

    items[1]["size"] = 999
    items[2]["mtime"] = 5
    assert loudness.scan(items, threading.Event()) == {"measured": 2, "skipped": 1, "failed": 0}
    assert calls == ["1.mp3", "2.mp3"]
    assert loudness.load()["id1"]["size"] == 999


def test_scan_failures_and_prune(monkeypatch):
    monkeypatch.setattr(loudness, "measure", lambda p, cancel=None: None if "1" in Path(p).name else
                        {"lufs": -9.0, "peak_db": 0.0})
    items = _items(3)
    assert loudness.scan(items, threading.Event()) == {"measured": 2, "skipped": 0, "failed": 1}
    assert set(loudness.load()) == {"id0", "id2"}
    # gone from the library: kept unless prune
    loudness.scan(items[:1], threading.Event())
    assert set(loudness.load()) == {"id0", "id2"}
    loudness.scan(items[:1], threading.Event(), prune=True)
    assert set(loudness.load()) == {"id0"}


def test_scan_saves_every_ten(monkeypatch):
    saves = []
    real = loudness._save
    monkeypatch.setattr(loudness, "_save", lambda d: (saves.append(len(d)), real(d)))
    monkeypatch.setattr(loudness, "measure", lambda p, cancel=None: {"lufs": -9.0, "peak_db": 0.0})
    loudness.scan(_items(25), threading.Event())
    assert saves == [10, 20, 25]


def test_scan_cancel_stops_between_files(monkeypatch):
    cancel = threading.Event()
    calls = []

    def fake(path, c=None):
        calls.append(path)
        if len(calls) == 2:
            cancel.set()
        return {"lufs": -9.0, "peak_db": 0.0}

    monkeypatch.setattr(loudness, "measure", fake)
    r = loudness.scan(_items(5), cancel)
    assert len(calls) == 2
    assert r["measured"] == 1                  # the one cancelled midway isn't counted
    assert set(loudness.load()) == {"id0"}     # what was measured is kept
    assert loudness.scan(_items(5), threading.Event())["skipped"] == 1


def test_measure_without_ffmpeg(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "find", lambda name, exe=None: None)
    assert loudness.measure(tmp_path / "x.mp3") is None


def _ffmpeg() -> str | None:
    p = Path(os.environ.get("LOCALAPPDATA", "")) / "BakinData" / "tools" / "ffmpeg" / "ffmpeg.exe"
    return str(p) if p.is_file() else shutil.which("ffmpeg")


@pytest.mark.skipif(_ffmpeg() is None, reason="ffmpeg not installed")
def test_measure_real_sine(monkeypatch, tmp_path):
    ff = _ffmpeg()
    wav = tmp_path / "sine.wav"
    subprocess.run([ff, "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=f=1000:d=3,volume=0.5", str(wav)], check=True, creationflags=tools.CREATE_NO_WINDOW)
    ref = subprocess.run([ff, "-nostdin", "-hide_banner", "-nostats", "-i", str(wav), "-af", "ebur128", "-f", "null", "-"],
                         capture_output=True, creationflags=tools.CREATE_NO_WINDOW).stderr.decode("utf-8", "replace")
    expected = float(re.findall(r"I:\s*(-?[\d.]+) LUFS", ref)[-1])

    monkeypatch.setattr(tools, "find", lambda name, exe=None: Path(ff))
    r = loudness.measure(wav)
    assert r is not None
    assert abs(r["lufs"] - expected) <= 1.0
    assert r["peak_db"] < 0
    # cancelled before it finishes: killed, nothing returned
    cancel = threading.Event()
    cancel.set()
    long = tmp_path / "long.wav"
    subprocess.run([ff, "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=f=440:d=600", str(long)], check=True, creationflags=tools.CREATE_NO_WINDOW)
    assert loudness.measure(long, cancel) is None
    assert loudness.measure(tmp_path / "missing.mp3") is None


def test_energy_axis():
    # PLAN 0.20.3: loud and even → intense; quiet with a wide range → calm
    assert loudness.energy(None) is None and loudness.energy({"lufs": -70.0}) is None
    calm = loudness.energy({"lufs": -24.0, "lra": 14.0})
    mid = loudness.energy({"lufs": -14.0})
    hot = loudness.energy({"lufs": -6.5, "lra": 3.0})
    assert 0.0 <= calm < mid < hot <= 1.0


def test_energy_mixes():
    from bakin import music
    items = [{"id": f"s{n}", "artist": f"a{n % 7}", "plays": 0} for n in range(30)]
    measured = {f"s{n}": {"lufs": -30.0 + n * 0.8, "lra": 5.0} for n in range(30)}
    got = {m["id"]: m["ids"] for m in music.mixes(items, now=1.8e9, log=[], skipped={}, measured=measured)}
    assert set(got["calm"]) == {f"s{n}" for n in range(10)}
    assert set(got["intense"]) == {f"s{n}" for n in range(20, 30)}
    few = {k: v for k, v in list(measured.items())[:music.ENERGY_MIN - 1]}
    got = {m["id"] for m in music.mixes(items, now=1.8e9, log=[], skipped={}, measured=few)}
    assert not got & {"calm", "intense"}
