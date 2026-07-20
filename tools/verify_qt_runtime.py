from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    if sys.version_info[:2] != (3, 11):
        raise SystemExit(f"Python 3.11 is required, found {sys.version.split()[0]}.")
    if sys.maxsize <= 2**32:
        raise SystemExit("64-bit Python is required.")

    from PySide6 import QtCore, QtGui, QtWidgets

    image = QtGui.QImage(2, 2, QtGui.QImage.Format.Format_ARGB32)
    if image.isNull():
        raise SystemExit("PySide6.QtGui loaded but could not create a QImage.")

    qt_root = Path(QtCore.__file__).resolve().parent
    print(f"Python: {sys.version}")
    print(f"PySide6: {QtCore.__version__}")
    print(f"Qt: {QtCore.qVersion()}")
    print(f"PySide6 root: {qt_root}")
    print(f"QtWidgets: {QtWidgets.__file__}")
    print(f"PATH head: {os.environ.get('PATH', '').split(os.pathsep)[:3]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
