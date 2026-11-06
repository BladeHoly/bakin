"""Secrets in the Windows Credential Manager (PLAN 0.13.4): today only the
optional Wallhaven API key. Never in files, logs or the page; read when
needed, through Windows' own API (advapi32 Cred*W, no extra package).

The value is stored as UTF-16, like other tools do, under a "generic"
credential named by `target` (e.g. "bakin-wallhaven"), for this user only.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2  # this user, this PC (not roamed)
ERROR_NOT_FOUND = 1168


class _CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _api():
    if sys.platform != "win32":
        raise OSError("El Administrador de credenciales solo existe en Windows.")
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))]
    adv.CredReadW.restype = wintypes.BOOL
    adv.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), wintypes.DWORD]
    adv.CredWriteW.restype = wintypes.BOOL
    adv.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    adv.CredDeleteW.restype = wintypes.BOOL
    adv.CredFree.argtypes = [ctypes.c_void_p]
    adv.CredFree.restype = None
    return adv


def get(target: str) -> str | None:
    """The secret saved under `target`, or None."""
    adv = _api()
    p = ctypes.POINTER(_CREDENTIAL)()
    if not adv.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(p)):
        if ctypes.get_last_error() == ERROR_NOT_FOUND:
            return None
        raise OSError("No se pudo leer el Administrador de credenciales de Windows.")
    try:
        cred = p.contents
        raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        return raw.decode("utf-16-le") or None
    finally:
        adv.CredFree(p)


def put(target: str, secret: str, user: str = "Bakin") -> None:
    adv = _api()
    blob = secret.encode("utf-16-le")
    buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    cred = _CREDENTIAL(Type=CRED_TYPE_GENERIC, TargetName=target, CredentialBlobSize=len(blob),
                       CredentialBlob=ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)),
                       Persist=CRED_PERSIST_LOCAL_MACHINE, UserName=user,
                       Comment="Bakin: clave de API (se puede borrar desde Bakin)")
    if not adv.CredWriteW(ctypes.byref(cred), 0):
        raise OSError("No se pudo guardar en el Administrador de credenciales de Windows.")


def delete(target: str) -> None:
    adv = _api()
    if not adv.CredDeleteW(target, CRED_TYPE_GENERIC, 0) and ctypes.get_last_error() != ERROR_NOT_FOUND:
        raise OSError("No se pudo borrar del Administrador de credenciales de Windows.")
