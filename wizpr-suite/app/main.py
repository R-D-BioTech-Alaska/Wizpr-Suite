from __future__ import annotations

import sys
from pathlib import Path

from PySide6 import QtWidgets

from ..core.config import get_default_app_dir
from ..core.logging_setup import setup_logging, get_logger
from ..ui.main_window import MainWindow


logger = get_logger("wizpr_suite")


def main() -> int:
    app_dir = get_default_app_dir()
    setup_logging(app_dir)

    app = QtWidgets.QApplication(sys.argv)
    win = MainWindow(app_dir=app_dir)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
