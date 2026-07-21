from __future__ import annotations

import importlib
import multiprocessing
import os
import sys
import tempfile

from pathlib import Path
from typing import BinaryIO
from ..core.config import get_default_app_dir
from ..core.logging_setup import get_logger, setup_logging

logger = get_logger("wizpr_suite")

def _run_self_test() -> int | None:
    if "--self-test" not in sys.argv:
        return None

    report_path = Path(tempfile.gettempdir()) / "WizprSuite-self-test.txt"
    modules = [
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "bleak",
        "httpx",
        "fastapi",
        "uvicorn",
        "openai",
        "wizpr_suite.core.memory",
        "wizpr_suite.core.desktop_tools",
        "numpy",
        "numpy.core._exceptions",
        "numpy.core._multiarray_umath",
        "faster_whisper",
        "ctranslate2",
        "av",
        "tokenizers",
    ]
    if os.name == "nt":
        modules.extend(
            [
                "winrt.windows.devices.bluetooth",
                "winrt.windows.devices.enumeration",
                "winrt.windows.devices.radios",
            ]
        )

    lines = [f"Python: {sys.version}", f"Executable: {sys.executable}"]
    try:
        for module_name in modules:
            module = importlib.import_module(module_name)
            if module_name == "numpy":
                values = module.arange(8, dtype=module.float32)
                if float(values.sum()) != 28.0:
                    raise RuntimeError("NumPy compiled core returned an invalid result")
                lines.append(f"OK: numpy {module.__version__}")
                continue
            lines.append(f"OK: {module_name}")

        resource_dir = Path(__file__).resolve().parents[1] / "resources"
        for resource_name in ("theme_dark.qss", "theme_light.qss", "wizpr_suite_logo.png", "wizpr_ring_card.png"):
            resource_path = resource_dir / resource_name
            if not resource_path.is_file():
                raise FileNotFoundError(f"Missing bundled resource: {resource_path}")
            lines.append(f"OK: {resource_path}")
    except BaseException as exc:
        lines.append(f"FAILED: {type(exc).__name__}: {exc}")
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return 1

    lines.append("SELF TEST PASSED")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0

def _run_transcription_worker() -> bool:
    if len(sys.argv) < 2 or sys.argv[1] != "--local-transcribe-worker":
        return False
    sys.argv = [sys.argv[0], *sys.argv[2:]]
    from ..tools.local_transcribe_worker import main as worker_main

    worker_main()
    return True

def _acquire_single_instance_lock(app_dir: Path) -> BinaryIO | None:
    app_dir.mkdir(parents=True, exist_ok=True)
    lock_path = app_dir / "wizpr_suite.lock"
    handle = lock_path.open("a+b")
    if os.name != "nt":
        return handle
    import msvcrt

    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle

def _release_single_instance_lock(handle: BinaryIO | None) -> None:
    if handle is None:
        return
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    finally:
        handle.close()

def main() -> int:
    multiprocessing.freeze_support()
    self_test_result = _run_self_test()
    if self_test_result is not None:
        return self_test_result
    if _run_transcription_worker():
        return 0

    from PySide6 import QtGui, QtWidgets

    from ..ui.main_window import MainWindow

    app_dir = get_default_app_dir()
    setup_logging(app_dir)

    app = QtWidgets.QApplication(sys.argv)
    icon_path = Path(__file__).resolve().parents[1] / "resources" / "wizpr_suite_logo.png"
    if icon_path.exists():
        app.setWindowIcon(QtGui.QIcon(str(icon_path)))

    lock = _acquire_single_instance_lock(app_dir)
    if lock is None:
        logger.warning("Wizpr Suite is already running; refusing to start a second instance.")
        QtWidgets.QMessageBox.warning(
            None,
            "Wizpr Suite already running",
            "Wizpr Suite is already open. Use the existing window so the ring connection is not split.",
        )
        return 2
    win = MainWindow(app_dir=app_dir)
    win.show()
    try:
        return app.exec()
    finally:
        _release_single_instance_lock(lock)

if __name__ == "__main__":
    raise SystemExit(main())
