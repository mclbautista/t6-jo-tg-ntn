"""Masked JSON-lines logging to stdout and owner-protected rotated storage."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TextIO

from jo_watchdog.core.masker import outbound_text


class MaskingFilter(logging.Filter):
    """Render arguments once, then mask every source-derived log message."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = outbound_text(record.getMessage()).text
        record.args = ()
        if record.stack_info:
            record.stack_info = outbound_text(record.stack_info).text
        return True


class JsonFormatter(logging.Formatter):
    """Emit one compact, machine-readable object per record."""

    def formatException(self, ei) -> str:  # noqa: N802
        return outbound_text(super().formatException(ei)).text

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, str] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "name": record.name,
            "level": record.levelname,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = outbound_text(record.stack_info).text
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class OwnerOnlyRotatingFileHandler(RotatingFileHandler):
    """Create the active log and future rotations with mode 0600."""

    def _open(self):
        previous_umask = os.umask(0o077)
        try:
            stream = super()._open()
        finally:
            os.umask(previous_umask)
        try:
            Path(self.baseFilename).chmod(0o600)
        except OSError:
            pass
        return stream


def configure_logging(
    *,
    log_path: str | Path,
    stream: TextIO | None = None,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    """Install masked JSON stdout and rotated persistent-file handlers."""
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass

    formatter = JsonFormatter()
    masking_filter = MaskingFilter()
    stdout = logging.StreamHandler(stream if stream is not None else sys.stdout)
    persistent = OwnerOnlyRotatingFileHandler(
        path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    for handler in (stdout, persistent):
        handler.setFormatter(formatter)
        handler.addFilter(masking_filter)

    root = logging.getLogger()
    for handler in root.handlers:
        handler.close()
    root.handlers[:] = [stdout, persistent]
    root.setLevel(logging.INFO)
