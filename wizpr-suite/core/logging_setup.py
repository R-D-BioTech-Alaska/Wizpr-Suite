from __future__ import annotations

import logging
from pathlib import Path

_LOGGER_NAME = "wizpr_suite"


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(_LOGGER_NAME if name is None else name)


def setup_logging(app_dir: Path, level: int = logging.INFO) -> None:
    app_dir.mkdir(parents=True, exist_ok=True)
    log_path = app_dir / "wizpr_suite.log"

    root = logging.getLogger(_LOGGER_NAME)
    root.setLevel(level)
    root.propagate = False

    if any(isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", "") == str(log_path) for h in root.handlers):
        return

    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(fmt)

    sh = logging.StreamHandler()
    sh.setLevel(level)
    sh.setFormatter(fmt)

    root.addHandler(fh)
    root.addHandler(sh)

    root.info("Logging initialized: %s", log_path)
