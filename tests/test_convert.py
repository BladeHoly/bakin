"""Converting files (bakin.convert): real Pillow; real ffmpeg when installed."""

import os
import subprocess
import threading
from pathlib import Path

import pytest
from PIL import Image

from bakin import convert, tools

REAL_FFMPEG = Path(os.environ.get("LOCALAPPDATA", "")) / "BakinData" / "tools" / "ffmpeg" / "ffmpeg.exe"
needs_ffmpeg = pytest.mark.skipif(not REAL_FFMPEG.is_file(), reason="ffmpeg not installed by Bakin on this machine")


@pytest.fixture
def ffmpeg(monkeypatch):
    real_find = tools.find
    monkeypatch.setattr(tools, "find", lambda name, exe=None: (REAL_FFMPEG.with_name(exe) if exe else REAL_FFMPEG)
                        if name == "ffmpeg" else real_find(name, exe))
    return REAL_FFMPEG


def opts(**kw):
    return convert.clean_options({"target": "png", **kw})


def run(files, o):
    events = []
    stats = convert.convert_files([Path(f) for f in files], o, events.append, threading.Event())
    return stats, events


def test_image_to_each_format_keeps_original_and_never_overwrites(tmp_path):
    src = tmp_path / "foto.png"
    Image.new("RGBA", (400, 300), (200, 30, 90, 128)).save(src)
    original = src.read_bytes()
    (tmp_path / "foto.jpg").write_bytes(b"already here")
    stats, _ = run([src], opts(target="jpg", quality=85))
    assert stats.done == 1 and Path(stats.outputs[0]).name == "foto (1).jpg"
    assert (tmp_path / "foto.jpg").read_bytes() == b"already here" and src.read_bytes() == original
    with Image.open(stats.outputs[0]) as im:
        assert im.format == "JPEG" and im.mode == "RGB" and im.size == (400, 300)
    for t, fmt in (("webp", "WEBP"), ("avif", "AVIF")):
        st, _ = run([src], opts(target=t))
        with Image.open(st.outputs[0]) as im:
            assert im.format == fmt
    assert not list(tmp_path.glob("*.bakin-tmp*"))


def test_image_resize_and_exif_rotation_baked_in(tmp_path):
    src = tmp_path / "rot.jpg"
    im = Image.new("RGB", (800, 400), (10, 120, 200))
    ex = Image.Exif()
    ex[0x0112] = 6  # rotate 90° on display
    im.save(src, "JPEG", exif=ex.tobytes())
    stats, _ = run([src], opts(target="png", max_size=200))
    with Image.open(stats.outputs[0]) as out:
        assert out.size == (100, 200)                      # rotated, then fit in 200
        assert out.getexif().get(0x0112, 1) == 1


def test_wrong_pairs_are_refused_per_file(tmp_path):
    img = tmp_path / "a.png"
    Image.new("RGB", (10, 10)).save(img)
    song = tmp_path / "s.mp3"
    song.write_bytes(b"x")
    assert convert.can_convert(img, "mp3") and convert.can_convert(song, "png")
    assert convert.can_convert(img, "png") == "a.png ya es PNG"
    assert convert.can_convert(tmp_path / "v.mp4", "mp3") is None    # audio out of a video is fine
    stats, events = run([img, song], opts(target="webp"))
    assert stats.done == 1 and stats.failed == 1
    assert [e["status"] for e in events if e["status"] != "working"] == ["done", "error"]


def test_bad_options_rejected():
    with pytest.raises(convert.ConvertError):
        convert.clean_options({"target": "exe"})
    o = convert.clean_options({"target": "mp3", "audio_quality": "999", "quality": 500, "video_quality": "x"})
    assert (o["audio_quality"], o["quality"], o["video_quality"]) == ("0", 100, "high")


def test_ffmpeg_args_shapes():
    a = convert.ffmpeg_args(Path("in.flac"), Path("out.mp3"), convert.clean_options({"target": "mp3"}))
    assert a[a.index("-c:a") + 1] == "libmp3lame" and "-q:a" in a and "attached_pic" in a
    a = convert.ffmpeg_args(Path("in.mp4"), Path("out.opus"), convert.clean_options({"target": "opus", "audio_quality": "128"}))
    assert "-vn" in a and a[a.index("-b:a") + 1] == "128k"
    a = convert.ffmpeg_args(Path("in.mp4"), Path("out.mkv"), convert.clean_options({"target": "mkv"}))
    assert a[a.index("-c") + 1] == "copy"                     # container change only
    a = convert.ffmpeg_args(Path("in.mkv"), Path("out.mp4"), convert.clean_options({"target": "mp4", "video_max_height": 720}))
    assert "libx264" in a and "scale=-2:'min(720,ih)'" in a
    assert a[a.index("-i") + 1] == "in.mkv" and a[-1] == "out.mp4"


def _make(ff, args, dest):
    subprocess.run([str(ff), "-hide_banner", "-loglevel", "error", "-y", *args, str(dest)], check=True,
                   capture_output=True, creationflags=0x08000000)
    return dest


@needs_ffmpeg
def test_real_audio_and_video_conversions(tmp_path, ffmpeg):
    song = _make(ffmpeg, ["-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:a", "libmp3lame",
                          "-metadata", "title=Prueba"], tmp_path / "tema.mp3")
    clip = _make(ffmpeg, ["-f", "lavfi", "-i", "testsrc=duration=1:size=640x360:rate=24", "-f", "lavfi",
                          "-i", "sine=duration=1", "-shortest", "-c:v", "libx264", "-c:a", "aac"], tmp_path / "clip.mp4")
    stats, events = run([song], convert.clean_options({"target": "flac"}))
    assert stats.done == 1 and stats.outputs[0].endswith("tema.flac")
    assert any(e["status"] == "working" for e in events)
    probe = tools.run_quick([str(ffmpeg.with_name("ffprobe.exe")), "-v", "quiet", "-show_entries",
                             "format_tags=title:stream=codec_name", "-of", "csv=p=0", stats.outputs[0]])
    assert "flac" in probe and "Prueba" in probe              # metadata kept
    for target, codec in (("opus", "opus"), ("webm", "vp9"), ("mkv", "h264"), ("gif", "gif")):
        st, ev = run([clip], convert.clean_options({"target": target, "video_max_height": 240 if target == "webm" else 0}))
        assert st.done == 1, ev
        out = tools.run_quick([str(ffmpeg.with_name("ffprobe.exe")), "-v", "quiet", "-show_entries",
                               "stream=codec_name,height", "-of", "csv=p=0", st.outputs[0]])
        assert codec in out, (target, out)
        if target == "webm":
            assert "240" in out


@needs_ffmpeg
def test_cancel_stops_ffmpeg_and_leaves_nothing(tmp_path, ffmpeg):
    clip = _make(ffmpeg, ["-f", "lavfi", "-i", "testsrc=duration=60:size=1920x1080:rate=30"], tmp_path / "largo.mp4")
    cancel = threading.Event()
    threading.Timer(1.5, cancel.set).start()
    stats = convert.convert_files([clip], convert.clean_options({"target": "webm"}), lambda e: None, cancel)
    assert stats.done == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["largo.mp4"]
