"""Secret redaction for logs, errors and anything printed."""

from __future__ import annotations

import logging
import re
import threading

SENSITIVE_NAMES = (
    # YouTube / Google sessions (the cookies Bakin keeps in the vault)
    "SID", "HSID", "SSID", "APISID", "SAPISID", "__Secure-1PSID", "__Secure-3PSID",
    "__Secure-1PAPISID", "__Secure-3PAPISID", "__Secure-1PSIDTS", "__Secure-3PSIDTS",
    "__Secure-1PSIDCC", "__Secure-3PSIDCC", "SIDCC", "LOGIN_INFO", "po_token",
    # generic
    "sessionid", "csrftoken", "authorization", "password", "apikey", "x-api-key",
)

_NAME_ALT = "|".join(re.escape(n) for n in SENSITIVE_NAMES)
_PATTERNS = (
    # name=value (cookies, query strings, form bodies)
    re.compile(rf"(?i)\b({_NAME_ALT})=([^;&\s'\"]+)"),
    # "name": "value" / 'name': 'value' (JSON, dict reprs)
    re.compile(rf"(?i)(['\"](?:{_NAME_ALT})['\"]\s*:\s*['\"])([^'\"]+)"),
    # Header lines
    re.compile(r"(?i)\b(cookie|set-cookie|x-api-key)(\s*[:=]\s*)([^\r\n]+)"),
)

_lock = threading.Lock()
_known_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Remember an exact secret value so it is scrubbed wherever it appears."""
    if value and len(value) >= 6:
        with _lock:
            _known_secrets.add(value)


def clear_secrets() -> None:
    with _lock:
        _known_secrets.clear()


def redact(text: object) -> str:
    s = str(text)
    with _lock:
        secrets = sorted(_known_secrets, key=len, reverse=True)
    for secret in secrets:
        if secret in s:
            s = s.replace(secret, "[REDACTED]")
    s = _PATTERNS[0].sub(lambda m: f"{m[1]}=[REDACTED]", s)
    s = _PATTERNS[1].sub(lambda m: f"{m[1]}[REDACTED]", s)
    s = _PATTERNS[2].sub(lambda m: f"{m[1]}{m[2]}[REDACTED]", s)
    return s


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        record.msg = redact(msg)
        record.args = None
        if record.exc_info and record.exc_info[1] is not None:
            # Render the traceback now, redacted, instead of letting the
            # formatter print raw exception text.
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def format_exception_redacted(exc: BaseException) -> str:
    import traceback
    return redact("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))


def install_excepthook(stream=None) -> None:
    """Route uncaught exceptions (main and worker threads) through the redactor."""
    import sys
    import traceback

    def hook(exc_type, exc, tb):
        text = redact("".join(traceback.format_exception(exc_type, exc, tb)))
        logging.getLogger("bakin.crash").error("unhandled exception:\n%s", text)
        (stream or sys.stderr).write(text)

    sys.excepthook = hook
    threading.excepthook = lambda a: hook(a.exc_type, a.exc_value, a.exc_traceback)


LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 3


def setup_logging(log_path, verbose: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)

    flt = RedactingFilter()
    # rotates at 2 MB, three old copies: the log never grows past ~8 MB
    from logging.handlers import RotatingFileHandler
    fh = RotatingFileHandler(log_path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")
    fh.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    fh.addFilter(flt)
    root.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setLevel(logging.DEBUG if verbose else logging.WARNING)
    sh.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    sh.addFilter(flt)
    root.addHandler(sh)

    # Third-party loggers are noisy at DEBUG and can echo tokens (urllib3
    # logs URLs). Keep them quiet unless verbose, and always behind the
    # redacting filter.
    for name in ("urllib3", "cookies"):
        logging.getLogger(name).setLevel(logging.DEBUG if verbose else logging.WARNING)
