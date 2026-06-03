"""Logging setup: rotating file + in-memory ring buffer for tray UI.

All modules just use `logging.getLogger("whisper2.<module>")` — this file
wires up handlers once, at startup, from the CLI or tray entry points.
"""
import logging
import os
import sys
from collections import deque
from logging.handlers import RotatingFileHandler

from paths import LOG_DIR, ensure_user_dirs

_RING: deque[str] = deque(maxlen=200)
_configured = False


class _RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _RING.append(self.format(record))
        except Exception:
            pass


def setup(debug: bool = False) -> None:
    """Configure the `whisper2` logger tree. Idempotent."""
    global _configured
    if _configured:
        return
    ensure_user_dirs()

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    root = logging.getLogger("whisper2")
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    # Don't bubble up to Python's root logger (would print to stderr twice in dev).
    root.propagate = False

    file_h = RotatingFileHandler(
        LOG_DIR / "whisper2.log",
        maxBytes=5_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_h.setFormatter(fmt)
    root.addHandler(file_h)

    ring_h = _RingHandler()
    ring_h.setFormatter(fmt)
    root.addHandler(ring_h)

    # In dev (not frozen) and on explicit debug flag, also stream to stderr so
    # console launches still show familiar output.
    want_stderr = (
        debug
        or os.environ.get("WHISPER2_DEBUG") == "1"
        or not getattr(sys, "frozen", False)
    )
    if want_stderr:
        stream_h = logging.StreamHandler(sys.stderr)
        stream_h.setFormatter(fmt)
        root.addHandler(stream_h)

    _configured = True


def recent_lines() -> list[str]:
    """Snapshot of the most recent log lines, for the tray's activity window."""
    return list(_RING)
