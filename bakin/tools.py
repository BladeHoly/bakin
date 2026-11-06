"""External tools for media downloads: yt-dlp, ffmpeg (+ffprobe) and Deno.

Bakin keeps its own copies in %LOCALAPPDATA%\\bakin\\tools, downloaded from
the projects' official GitHub releases and checked against the SHA-256 the
release publishes before anything is installed. If a tool is not installed
there, one found on PATH is used instead.

yt-dlp is updated on its own schedule (YouTube changes often break it), not
with Bakin releases.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from .paths import app_dir

CREATE_NO_WINDOW = 0x08000000
UA = "Bakin (tools updater)"


@dataclass(frozen=True)
class Tool:
    name: str
    exes: tuple[str, ...]          # files installed; the first is the main one
    url: str                       # .exe or .zip
    sums_url: str
    sums_key: str                  # file name to look up in the sums file ("" = only hash in it)
    size_hint: str


TOOLS = {
    "yt-dlp": Tool("yt-dlp", ("yt-dlp.exe",),
                   "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe",
                   "https://github.com/yt-dlp/yt-dlp/releases/latest/download/SHA2-256SUMS",
                   "yt-dlp.exe", "23 MB"),
    "ffmpeg": Tool("ffmpeg", ("ffmpeg.exe", "ffprobe.exe"),
                   "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
                   "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/checksums.sha256",
                   "ffmpeg-master-latest-win64-gpl.zip", "200 MB"),
    "deno": Tool("deno", ("deno.exe",),
                 "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip",
                 "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip.sha256sum",
                 "", "43 MB"),
}


class ToolError(Exception):
    pass


def tools_dir() -> Path:
    return app_dir() / "tools"


def _state_path() -> Path:
    return tools_dir() / "state.json"


def _state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    tools_dir().mkdir(parents=True, exist_ok=True)
    _state_path().write_text(json.dumps(state, indent=1), encoding="utf-8")


def managed_path(name: str, exe: str | None = None) -> Path:
    tool = TOOLS[name]
    return tools_dir() / name / (exe or tool.exes[0])


def find(name: str, exe: str | None = None) -> Path | None:
    """Bakin's copy first, then PATH."""
    p = managed_path(name, exe)
    if p.is_file():
        return p
    found = shutil.which(exe or TOOLS[name].exes[0])
    return Path(found) if found else None


def child_env() -> dict:
    """Environment for running a tool from Bakin's engine.

    yt-dlp.exe is itself a PyInstaller program: launched from Bakin's frozen
    engine it would otherwise pick up the engine's PyInstaller variables."""
    return {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}


def kill_tree(pid: int) -> None:
    """End a process and everything it started (yt-dlp.exe runs as two
    processes; killing only the first leaves the second holding our pipes)."""
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                   stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)


def run_quick(cmd: list[str], timeout: float = 20) -> str | None:
    """Run a short command; never hangs past `timeout`."""
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env=child_env(), creationflags=CREATE_NO_WINDOW)
    except OSError:
        return None
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc.pid)
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return None
    return out.decode("utf-8", "replace")


def version(name: str, *, refresh: bool = False) -> str | None:
    """Tool version, cached (running the tools on every status check is slow)."""
    p = find(name)
    if p is None:
        return None
    st = _state()
    key = f"{p}|{p.stat().st_mtime_ns if p.exists() else 0}"
    cached = (st.get(name) or {})
    if not refresh and cached.get("version_key") == key and cached.get("version"):
        return cached["version"]
    args = {"yt-dlp": ["--version"], "ffmpeg": ["-version"], "deno": ["--version"]}[name]
    out = run_quick([str(p), *args])
    if out is None:
        return None
    v = _parse_version(out)
    if v:
        st.setdefault(name, {}).update(version=v, version_key=key)
        _save_state(st)
    return v


def _parse_version(out: str) -> str | None:
    first = (out.strip().splitlines() or [""])[0].strip()
    if not first:
        return None
    # "2026.10.22" / "ffmpeg version N-121... Copyright" / "deno 2.5.4 (stable, ...)"
    first = re.sub(r"^(ffmpeg version|deno)\s+", "", first)
    return first.split()[0] if first else None


def status() -> dict:
    st = _state()
    out = {}
    for name, tool in TOOLS.items():
        p = find(name)
        out[name] = {"installed": p is not None, "managed": managed_path(name).is_file(),
                     "path": str(p) if p else None, "version": version(name) if p else None,
                     "size": tool.size_hint, "checked_at": st.get(name, {}).get("checked_at")}
    return out


def _expected_sha(tool: Tool, session: requests.Session) -> str:
    r = session.get(tool.sums_url, timeout=30)
    r.raise_for_status()
    for line in r.text.splitlines():
        hexes = re.findall(r"\b[0-9a-fA-F]{64}\b", line)
        if hexes and (not tool.sums_key or line.strip().endswith(tool.sums_key)):
            return hexes[0].lower()
    raise ToolError(f"no se encontró el checksum de {tool.name} en su release")


def latest_sha(name: str) -> str:
    with requests.Session() as s:
        s.headers["User-Agent"] = UA
        return _expected_sha(TOOLS[name], s)


def update_available(name: str) -> bool | None:
    """True/False against the latest release; None if Bakin doesn't manage it."""
    st = _state().get(name) or {}
    if not managed_path(name).is_file() or not st.get("sha256"):
        return None
    return latest_sha(name) != st["sha256"]


def install(name: str, progress: Callable[[int, int | None], None] = lambda d, t: None,
            cancel=None) -> str:
    """Download, verify (SHA-256) and install a tool. Returns its version."""
    tool = TOOLS[name]
    dest = tools_dir() / name
    tools_dir().mkdir(parents=True, exist_ok=True)
    with requests.Session() as s:
        s.headers["User-Agent"] = UA
        expected = _expected_sha(tool, s)
        fd, tmp = tempfile.mkstemp(prefix=f"{name}-", suffix=".download", dir=tools_dir())
        try:
            h = hashlib.sha256()
            with os.fdopen(fd, "wb") as fp, s.get(tool.url, stream=True, timeout=60) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length") or 0) or None
                done = 0
                for chunk in r.iter_content(1 << 20):
                    if cancel is not None and cancel.is_set():
                        raise ToolError("cancelado")
                    fp.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    progress(done, total)
            if h.hexdigest() != expected:
                raise ToolError(f"el archivo descargado de {name} no coincide con su checksum oficial; no se instaló")
            staging = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=tools_dir()))
            try:
                if tool.url.endswith(".zip"):
                    with zipfile.ZipFile(tmp) as z:
                        for exe in tool.exes:
                            member = next((m for m in z.namelist() if m.lower().endswith("/" + exe) or m.lower() == exe), None)
                            if member is None:
                                raise ToolError(f"{exe} no está en el paquete de {name}")
                            with z.open(member) as src, open(staging / exe, "wb") as out:
                                shutil.copyfileobj(src, out)
                else:
                    shutil.copyfile(tmp, staging / tool.exes[0])
                old = dest.with_name(dest.name + ".old")
                shutil.rmtree(old, ignore_errors=True)
                if dest.exists():
                    os.replace(dest, old)
                os.replace(staging, dest)
                shutil.rmtree(old, ignore_errors=True)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        finally:
            Path(tmp).unlink(missing_ok=True)
    st = _state()
    st[name] = {"sha256": expected, "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    _save_state(st)
    return version(name, refresh=True) or "?"


def mark_checked(name: str) -> None:
    st = _state()
    st.setdefault(name, {})["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _save_state(st)


def missing(required=("yt-dlp", "ffmpeg", "deno")) -> list[str]:
    return [n for n in required if find(n) is None]
