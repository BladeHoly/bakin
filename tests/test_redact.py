"""Secrets never reach logs, errors or tracebacks (bakin.redact)."""

import io
import logging

from bakin import redact

SECRET = "1234567890%3ASECRETSECRETSECRET%3A12"


def test_redaction_patterns():
    text = (f"Cookie: sessionid={SECRET}; csrftoken=abc123456\n"
            "Authorization: Bearer abcdefabcdef1234 url?token=AVqLSD2&x=1")
    out = redact.redact(text)
    assert SECRET not in out and "abc123456" not in out


def test_registered_secret_scrubbed_anywhere(caplog):
    redact.register_secret(SECRET)
    logger = logging.getLogger("bakin.test")
    logger.addFilter(redact.RedactingFilter())
    with caplog.at_level(logging.INFO):
        logger.info("weird place %s", f"xx{SECRET}yy")
    assert SECRET not in caplog.text


def test_exception_tracebacks_are_redacted(caplog):
    redact.register_secret("supersecretvalue")
    logger = logging.getLogger("bakin.test2")
    logger.addFilter(redact.RedactingFilter())
    try:
        raise RuntimeError("failed with supersecretvalue")
    except RuntimeError:
        with caplog.at_level(logging.ERROR):
            logger.exception("boom")
    assert "supersecretvalue" not in caplog.text


def test_excepthook_redacts_uncaught_exceptions():
    import sys
    out = io.StringIO()
    old = sys.excepthook
    try:
        redact.register_secret(SECRET)
        redact.install_excepthook(stream=out)
        try:
            raise RuntimeError(f"boom sessionid={SECRET}")
        except RuntimeError as exc:
            sys.excepthook(type(exc), exc, exc.__traceback__)
    finally:
        sys.excepthook = old
    assert "RuntimeError" in out.getvalue() and SECRET not in out.getvalue()


def test_log_rotates_and_stays_small(tmp_path, monkeypatch):
    monkeypatch.setattr(redact, "LOG_MAX_BYTES", 50_000)
    log = tmp_path / "bakin.log"
    redact.setup_logging(log)
    try:
        logger = logging.getLogger("bakin.test.rotate")
        for i in range(2000):
            logger.info("line %d %s", i, "x" * 100)
        files = sorted(p.name for p in tmp_path.iterdir())
        assert "bakin.log.1" in files and "bakin.log.4" not in files        # three old copies at most
        assert all(p.stat().st_size <= 60_000 for p in tmp_path.iterdir())
    finally:
        for h in list(logging.getLogger().handlers):
            h.close()
            logging.getLogger().removeHandler(h)


def test_log_size_for_consumo(tmp_path, monkeypatch):
    from bakin import service
    svc = service.Service()
    info = svc.log_info()
    assert info["bytes"] >= 0 and info["folder"]


def test_google_session_cookies_are_scrubbed():
    # security review 2026-11-07: the list still named the old photo site's cookies only
    line = "cookie jar: SAPISID=abc123def456; __Secure-3PSID=g.a000xyz; LOGIN_INFO=AFmmF2sw"
    out = redact.redact(line)
    assert "abc123def456" not in out and "g.a000xyz" not in out and "AFmmF2sw" not in out
