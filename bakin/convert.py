"""Convert files to another format: images with Pillow, audio and video with
the ffmpeg Bakin manages.

Always writes new files next to the original (or into a chosen folder) and
never overwrites anything: a name that is taken gets " (1)", " (2)"... The
original is only read.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import tools
from .library import kind_of

CREATE_NO_WINDOW = 0x08000000

IMAGE_TARGETS = ("jpg", "png", "webp", "avif")
AUDIO_TARGETS = ("mp3", "m4a", "opus", "flac", "ogg", "wav")
VIDEO_TARGETS = ("mp4", "webm", "mkv", "gif")
PIL_READS = {"jpg", "jpeg", "jfif", "png", "webp", "avif", "gif", "bmp", "tif", "tiff"}

# What each kind of file can become ("audio" from a video extracts its sound).
ALLOWED = {
    "image": {"image": IMAGE_TARGETS},
    "audio": {"audio": AUDIO_TARGETS},
    "video": {"video": VIDEO_TARGETS, "audio": AUDIO_TARGETS},
}


class ConvertError(Exception):
    pass


def clean_options(o: dict) -> dict:
    def num(k, lo, hi, d):
        try:
            return max(lo, min(hi, int(o.get(k, d))))
        except (TypeError, ValueError):
            return d
    target = str(o.get("target") or "").lower()
    if target not in IMAGE_TARGETS + AUDIO_TARGETS + VIDEO_TARGETS:
        raise ConvertError("Formato de salida inválido.")
    q = str(o.get("audio_quality") or "0")
    return {
        "target": target,
        "quality": num("quality", 1, 100, 90),          # images (JPG/WebP/AVIF)
        "max_size": num("max_size", 0, 20000, 0),        # images: longest side; 0 = keep
        "audio_quality": q if q in ("0", "320", "192", "128") else "0",
        "video_max_height": num("video_max_height", 0, 4320, 0),
        "video_quality": str(o.get("video_quality") or "high") if str(o.get("video_quality")) in ("high", "medium", "small") else "high",
        "keep_metadata": bool(o.get("keep_metadata", True)),
        "out_dir": str(o.get("out_dir") or "").strip(),  # "" = next to each original
    }


def target_kind(target: str) -> str:
    return "image" if target in IMAGE_TARGETS else "audio" if target in AUDIO_TARGETS else "video"


def can_convert(src: Path, target: str) -> str | None:
    """Why `src` can't become `target`, or None."""
    kind = kind_of(src.suffix[1:])
    allowed = ALLOWED.get(kind, {})
    if target_kind(target) not in allowed:
        what = {"image": "una imagen", "audio": "un audio", "video": "un video"}.get(kind, "este archivo")
        return f"{src.name}: {what} no se puede convertir a {target.upper()}"
    if src.suffix[1:].lower() == target or (src.suffix.lower() in (".jpeg", ".jfif") and target == "jpg"):
        return f"{src.name} ya es {target.upper()}"
    if kind == "image" and src.suffix[1:].lower() not in PIL_READS:
        return f"{src.name}: Bakin no puede leer imágenes {src.suffix[1:].upper()}"
    return None


def output_path(src: Path, target: str, out_dir: str) -> Path:
    folder = Path(out_dir) if out_dir else src.parent
    ext = "jpg" if target == "jpg" else target
    dest = folder / f"{src.stem}.{ext}"
    n = 1
    while dest.exists():
        dest = folder / f"{src.stem} ({n}).{ext}"
        n += 1
    return dest


# ------------------------------------------------------------------ images

def convert_image(src: Path, dest: Path, o: dict) -> None:
    from PIL import Image, ImageOps
    with Image.open(src) as im:
        icc = im.info.get("icc_profile")
        exif = im.info.get("exif")
        im = ImageOps.exif_transpose(im)  # bake the rotation in (EXIF orientation is reset below)
        if o["max_size"] and max(im.size) > o["max_size"]:
            im.thumbnail((o["max_size"], o["max_size"]), Image.LANCZOS)
        t = o["target"]
        if t == "jpg" and im.mode not in ("RGB", "L"):
            im = _flatten(im)
        elif t in ("webp", "avif", "png") and im.mode not in ("RGB", "RGBA", "L", "LA", "P"):
            im = im.convert("RGBA" if "A" in im.mode else "RGB")
        kw = {}
        if o["keep_metadata"]:
            if icc:
                kw["icc_profile"] = icc
            if exif and t in ("jpg", "webp", "avif", "png"):
                ex = Image.Exif()
                ex.load(exif)
                ex[0x0112] = 1  # orientation: already applied
                kw["exif"] = ex.tobytes()
        if t == "jpg":
            im.save(dest, "JPEG", quality=o["quality"], optimize=True, progressive=True, subsampling=0 if o["quality"] >= 90 else 2, **kw)
        elif t == "png":
            im.save(dest, "PNG", optimize=True, **kw)
        elif t == "webp":
            im.save(dest, "WEBP", quality=o["quality"], method=6, **kw)
        else:
            im.save(dest, "AVIF", quality=o["quality"], **kw)


def _flatten(im):
    from PIL import Image
    rgba = im.convert("RGBA")
    bg = Image.new("RGB", rgba.size, (255, 255, 255))
    bg.paste(rgba, mask=rgba.getchannel("A"))
    return bg


# ----------------------------------------------------------- audio / video

AUDIO_CODEC = {
    "mp3": ["-c:a", "libmp3lame"], "m4a": ["-c:a", "aac"], "opus": ["-c:a", "libopus"],
    "flac": ["-c:a", "flac"], "ogg": ["-c:a", "libvorbis"], "wav": ["-c:a", "pcm_s16le"],
}
COVER_OK = {"mp3", "m4a", "flac"}  # containers that keep embedded cover art


def audio_quality_args(target: str, q: str) -> list[str]:
    if target in ("flac", "wav"):
        return []
    if q == "0":  # best VBR, like yt-dlp's --audio-quality 0
        return {"mp3": ["-q:a", "0"], "m4a": ["-b:a", "256k"], "opus": ["-b:a", "192k"],
                "ogg": ["-q:a", "8"]}[target]
    return ["-b:a", f"{q}k"]


VIDEO_CRF = {"high": {"mp4": 18, "webm": 28}, "medium": {"mp4": 23, "webm": 33}, "small": {"mp4": 28, "webm": 40}}


def ffmpeg_args(src: Path, dest: Path, o: dict) -> list[str]:
    t = o["target"]
    args = ["-hide_banner", "-nostdin", "-y", "-i", str(src)]
    meta = ["-map_metadata", "0"] if o["keep_metadata"] else ["-map_metadata", "-1"]
    if t in AUDIO_TARGETS:
        if t in COVER_OK and o["keep_metadata"] and kind_of(src.suffix[1:]) == "audio":
            args += ["-map", "0:a", "-map", "0:v?", "-c:v", "copy", "-disposition:v", "attached_pic"]
        else:
            args += ["-map", "0:a", "-vn"]
        args += AUDIO_CODEC[t] + audio_quality_args(t, o["audio_quality"]) + meta
    else:
        h = o["video_max_height"]
        scale = [f"scale=-2:'min({h},ih)'"] if h else []
        if t == "gif":
            fps = "fps=12," + (f"scale=-2:'min({h},ih)':flags=lanczos," if h else "scale=-2:'min(480,ih)':flags=lanczos,")
            args += ["-vf", fps + "split[a][b];[a]palettegen[p];[b][p]paletteuse", "-an"]
        elif t == "mkv" and not h:
            args += ["-map", "0", "-c", "copy"] + meta  # change the container only: lossless and fast
        elif t == "mp4" or t == "mkv":
            crf = VIDEO_CRF[o["video_quality"]]["mp4"]
            args += ["-c:v", "libx264", "-preset", "medium", "-crf", str(crf), "-pix_fmt", "yuv420p",
                     "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"] + meta
            if scale:
                args += ["-vf", scale[0]]
        else:  # webm
            crf = VIDEO_CRF[o["video_quality"]]["webm"]
            args += ["-c:v", "libvpx-vp9", "-crf", str(crf), "-b:v", "0", "-row-mt", "1",
                     "-c:a", "libopus", "-b:a", "160k"] + meta
            if scale:
                args += ["-vf", scale[0]]
    return args + ["-progress", "pipe:1", "-nostats", str(dest)]


def duration_of(src: Path) -> float | None:
    probe = tools.find("ffmpeg", "ffprobe.exe")
    if probe is None:
        return None
    out = tools.run_quick([str(probe), "-v", "quiet", "-print_format", "json", "-show_entries",
                           "format=duration", str(src)], timeout=30)
    try:
        return float(json.loads(out or "{}")["format"]["duration"])
    except (ValueError, KeyError, TypeError):
        return None


def run_ffmpeg(src: Path, dest: Path, o: dict, progress: Callable[[float], None],
               cancel: threading.Event) -> None:
    ff = tools.find("ffmpeg")
    if ff is None:
        raise ConvertError("Falta ffmpeg: instálalo desde «Herramientas».")
    total = duration_of(src)
    proc = subprocess.Popen([str(ff), *ffmpeg_args(src, dest, o)], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=tools.child_env(),
                            creationflags=CREATE_NO_WINDOW)
    err_tail: list[str] = []

    def drain():
        for raw in proc.stderr:
            err_tail.append(raw.decode("utf-8", "replace").rstrip())
            del err_tail[:-15]
    threading.Thread(target=drain, daemon=True).start()

    def watch():
        while proc.poll() is None:
            if cancel.wait(0.3):
                tools.kill_tree(proc.pid)
                return
    threading.Thread(target=watch, daemon=True).start()
    for raw in proc.stdout:
        line = raw.decode("utf-8", "replace").strip()
        if line.startswith("out_time_us=") and total:
            try:
                progress(min(1.0, int(line.split("=", 1)[1]) / 1e6 / total))
            except ValueError:
                pass
    code = proc.wait()
    if cancel.is_set():
        raise ConvertError("cancelado")
    if code != 0:
        msg = next((l for l in reversed(err_tail) if l and not l.startswith("[")), "") or (err_tail[-1] if err_tail else "")
        raise ConvertError(f"ffmpeg no pudo convertirlo ({msg[:200]})")


# ------------------------------------------------------------------- job

@dataclass
class ConvertStats:
    done: int = 0
    failed: int = 0
    outputs: list = field(default_factory=list)


def convert_files(files: list[Path], opts: dict, emit: Callable[[dict], None],
                  cancel: threading.Event) -> ConvertStats:
    stats = ConvertStats()
    for i, src in enumerate(files, 1):
        if cancel.is_set():
            break
        base = {"index": i, "count": len(files), "name": src.name, "src": str(src)}
        why = can_convert(src, opts["target"])
        if why:
            stats.failed += 1
            emit({"kind": "convert_file", **base, "status": "error", "message": why})
            continue
        out_dir = Path(opts["out_dir"]) if opts["out_dir"] else src.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = output_path(src, opts["target"], opts["out_dir"])
        tmp = dest.with_name(f".{dest.stem}.bakin-tmp{dest.suffix}")
        emit({"kind": "convert_file", **base, "status": "working", "progress": 0})
        last = [0.0]

        def prog(f, base=base):
            now = time.monotonic()
            if now - last[0] > 0.3:
                last[0] = now
                emit({"kind": "convert_file", **base, "status": "working", "progress": round(f, 3)})
        try:
            if target_kind(opts["target"]) == "image":
                convert_image(src, tmp, opts)
            else:
                run_ffmpeg(src, tmp, opts, prog, cancel)
            if dest.exists():  # appeared meanwhile: never overwrite
                dest = output_path(src, opts["target"], opts["out_dir"])
            os.replace(tmp, dest)
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            if cancel.is_set():
                break
            stats.failed += 1
            msg = str(exc) if isinstance(exc, ConvertError) else f"{exc.__class__.__name__}: {exc}"
            emit({"kind": "convert_file", **base, "status": "error", "message": msg[:300]})
            continue
        stats.done += 1
        stats.outputs.append(str(dest))
        emit({"kind": "convert_file", **base, "status": "done", "progress": 1, "out": str(dest),
              "out_name": dest.name, "size": dest.stat().st_size})
    return stats


def describe(path: Path) -> dict:
    kind = kind_of(path.suffix[1:])
    st = path.stat()
    return {"path": str(path), "name": path.name, "kind": kind, "ext": path.suffix[1:].lower(),
            "size": st.st_size, "mtime": int(st.st_mtime)}


def formats() -> dict:
    from PIL import features
    return {"image": [t for t in IMAGE_TARGETS if t != "avif" or features.check("avif")],
            "audio": list(AUDIO_TARGETS), "video": list(VIDEO_TARGETS),
            "ffmpeg": tools.find("ffmpeg") is not None}


def is_allowed_input(p: Path) -> bool:
    return p.is_file() and not re.search(r"\.bakin-tmp\.", p.name)
