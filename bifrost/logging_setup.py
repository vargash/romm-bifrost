"""Structured file-based logging for headless/unattended operation."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_LOG_BACKUP_COUNT = 5
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


def _log_dir() -> Path:
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    base = (
        Path(xdg_data_home).expanduser()
        if xdg_data_home
        else Path.home() / ".local" / "share"
    )
    return base / "bifrost" / "logs"


def setup_file_logging(verbose: bool = False) -> Path:
    """Configure rotating file logger. Returns the log file path.

    Idempotent: safe to call multiple times per process.
    """
    log_dir = _log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "bifrost.log"

    level = logging.DEBUG if verbose else logging.INFO

    logger = logging.getLogger("bifrost")
    if logger.handlers:
        logger.setLevel(level)
        return log_path

    handler = RotatingFileHandler(
        log_path,
        maxBytes=_LOG_MAX_BYTES,
        backupCount=_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))

    logger.setLevel(level)
    logger.addHandler(handler)
    logger.propagate = False

    return log_path
