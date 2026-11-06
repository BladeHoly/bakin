"""Private application directory (outside any repository)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "bakin"
# On Windows the data folder must not be %LOCALAPPDATA%\Bakin: that is the
# installer's default install folder (paths are case-insensitive there).
WINDOWS_DATA_NAME = "BakinData"


def app_dir() -> Path:
    """Per-user private directory for logs, config and non-secret caches.

    Secrets are NOT stored here in plain form: small ones go to the OS credential
    manager, big ones (cookies) to vault/, encrypted with DPAPI (vault.py).
    Override with BAKIN_HOME (used by tests).
    """
    if override := os.environ.get("BAKIN_HOME"):
        base = Path(override)
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / WINDOWS_DATA_NAME
        if not base.exists():
            _move_out_of_install_folder(base)
        moved = redirect_target(base)
        if moved is not None:
            return ensure_private_dir(moved)
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_NAME
    if not base.exists() and not os.environ.get("BAKIN_HOME"):
        _copy_legacy_settings(base)
    return ensure_private_dir(base)


REDIRECT = "redirect.txt"


def default_dir() -> Path:
    """Where Bakin's data lives unless it was moved (Windows: %LOCALAPPDATA%\BakinData)."""
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / WINDOWS_DATA_NAME


def redirect_target(base: Path) -> Path | None:
    """The data folder moved elsewhere (PLAN 0.18.4): `redirect.txt` in the usual one holds its
    path. None when there's no redirect, or its folder isn't there now (a disk unplugged): then the
    usual folder is used and Bakin says so (`redirect_missing`)."""
    try:
        text = (base / REDIRECT).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    target = Path(text)
    return target if text and target.is_absolute() and target.is_dir() else None


def redirect_missing() -> str:
    """The moved data folder's path when it can't be found now, else ""."""
    if os.environ.get("BAKIN_HOME") or sys.platform != "win32":
        return ""
    base = default_dir()
    try:
        text = (base / REDIRECT).read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return text if text and redirect_target(base) is None else ""


# What Bakin itself kept in %LOCALAPPDATA%\bakin before 0.4 (anything else
# there belongs to the installed app and is left alone).
_DATA_ENTRIES = ("ui-settings.json", "config.toml", "gdl-cache.sqlite3", "logs", "thumbs", "tools",
                 "library", "library-folders.json", "library-moves.json", "media.json")


def _move_out_of_install_folder(new_base: Path) -> None:
    import shutil
    old = new_base.with_name(APP_NAME)
    if not old.is_dir():
        return
    ensure_private_dir(new_base)
    for name in _DATA_ENTRIES:
        src = old / name
        if src.exists():
            try:
                os.replace(src, new_base / name)
            except OSError:
                if src.is_dir():
                    shutil.copytree(src, new_base / name, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, new_base / name)
    try:
        old.rmdir()  # only if nothing else is left in it
    except OSError:
        pass


LEGACY_APP_NAME = "igmax"
_LEGACY_FILES = ("config.toml", "ui-settings.json")


def _copy_legacy_settings(new_base: Path) -> None:
    """First run after the rename: bring over non-secret settings from the
    old data folder (logs and caches are simply rebuilt)."""
    import shutil
    old = new_base.with_name(LEGACY_APP_NAME)
    if not old.is_dir():
        return
    ensure_private_dir(new_base)
    for name in _LEGACY_FILES:
        if (old / name).is_file():
            shutil.copy2(old / name, new_base / name)


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(path, 0o700)
    # On Windows, %LOCALAPPDATA% already carries a per-user ACL
    # (user, SYSTEM, Administrators); new subdirectories inherit it.
    return path


def is_inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def user_downloads_dir() -> Path:
    """The user's Downloads folder (where Windows really has it, even if moved)."""
    if sys.platform == "win32":
        try:
            import ctypes
            import uuid
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("data", ctypes.c_byte * 16)]

            folder_id = GUID()
            folder_id.data[:] = uuid.UUID("374DE290-123F-4565-9164-39C4925E467B").bytes_le  # FOLDERID_Downloads
            out = ctypes.c_wchar_p()
            shell32 = ctypes.windll.shell32
            if shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(out)) == 0:
                try:
                    return Path(out.value)
                finally:
                    ctypes.windll.ole32.CoTaskMemFree(out)
        except Exception:
            pass
    return Path.home() / "Downloads"


def config_file() -> Path:
    return app_dir() / "config.toml"


def log_file() -> Path:
    return ensure_private_dir(app_dir() / "logs") / "bakin.log"
