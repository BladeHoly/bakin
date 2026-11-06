"""Secrets too big for the Credential Manager (2026-11-07: the YouTube cookies a preset logs in
with, ~4–10 KB against its ~2.5 KB limit): files in BakinData/vault, encrypted with Windows'
DPAPI (`CryptProtectData`), tied to this Windows user. Copied to another PC or another user they
can't be read. Never in logs, errors or the page.

A program that needs the plain file (yt-dlp's `--cookies`) gets a copy that lives only while it
runs: `scratch()` writes it, `wipe()` overwrites it with zeros and deletes it.
"""

from __future__ import annotations

import ctypes
import os
import secrets
import sys
from ctypes import wintypes
from pathlib import Path

from .paths import app_dir

# Mixed into the encryption: another program of this user calling DPAPI without it can't read
# Bakin's files (not secret by itself; it only keeps them Bakin's).
_ENTROPY = b"Bakin vault v1"


class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _api():
    if sys.platform != "win32":
        raise OSError("La bóveda de Bakin usa la protección de Windows (DPAPI).")
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    for fn in (crypt.CryptProtectData, crypt.CryptUnprotectData):
        fn.argtypes = [ctypes.POINTER(_BLOB), ctypes.c_void_p, ctypes.POINTER(_BLOB), ctypes.c_void_p,
                       ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_BLOB)]
        fn.restype = wintypes.BOOL
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    return crypt, kernel


def _blob(data: bytes) -> tuple[_BLOB, object]:
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data) if data else (ctypes.c_ubyte * 1)()
    return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))), buf


CRYPTPROTECT_UI_FORBIDDEN = 0x1


def _run(protect: bool, data: bytes) -> bytes:
    crypt, kernel = _api()
    src, _keep = _blob(data)
    ent, _keep2 = _blob(_ENTROPY)
    out = _BLOB()
    fn = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    desc = ctypes.c_wchar_p("Bakin") if protect else None
    if not fn(ctypes.byref(src), desc, ctypes.byref(ent), None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise OSError("Windows no pudo " + ("cifrar" if protect else "descifrar") + " el secreto de la bóveda.")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel.LocalFree(out.pbData)


def _dir() -> Path:
    return app_dir() / "vault"


def _file(name: str) -> Path:
    if not name.replace("-", "").isalnum():
        raise ValueError("nombre de secreto inválido")
    return _dir() / f"{name}.bin"


def put(name: str, data: bytes) -> None:
    d = _dir()
    d.mkdir(parents=True, exist_ok=True)
    enc = _run(True, data)
    tmp = d / f".{name}.{secrets.token_hex(4)}.tmp"
    tmp.write_bytes(enc)
    os.replace(tmp, _file(name))


def get(name: str) -> bytes | None:
    try:
        enc = _file(name).read_bytes()
    except FileNotFoundError:
        return None
    return _run(False, enc)


def has(name: str) -> bool:
    return _file(name).is_file()


def mtime(name: str) -> float | None:
    try:
        return _file(name).stat().st_mtime
    except OSError:
        return None


def delete(name: str) -> None:
    _file(name).unlink(missing_ok=True)


def scratch(data: bytes, suffix: str = ".txt") -> Path:
    """A plain copy for a program that needs a file; `wipe()` it as soon as that program ends.
    Left over by a crash, the next start removes it (`sweep()`)."""
    d = _dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"run-{secrets.token_hex(8)}{suffix}"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as fp:
        fp.write(data)
    return p


def wipe(p: Path) -> None:
    """Overwrites a plain copy with zeros, then deletes it."""
    try:
        n = p.stat().st_size
        with open(p, "r+b") as fp:
            fp.write(b"\0" * n)
            fp.flush()
            os.fsync(fp.fileno())
    except OSError:
        pass
    p.unlink(missing_ok=True)


def sweep() -> None:
    """Plain copies a crash left behind."""
    for p in _dir().glob("run-*"):
        wipe(p)
