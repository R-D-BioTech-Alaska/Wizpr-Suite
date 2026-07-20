from __future__ import annotations

import runpy
from pathlib import Path


def test_launcher_imports_packaged_main() -> None:
    root = Path(__file__).resolve().parents[1]
    namespace = runpy.run_path(str(root / "wizpr_launcher.py"), run_name="wizpr_launcher_test")
    assert namespace["main"].__module__ == "wizpr_suite.app.main"
