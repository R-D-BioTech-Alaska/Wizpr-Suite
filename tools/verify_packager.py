from __future__ import annotations

import importlib.metadata
import importlib.util
import sys


def main() -> int:
    import PyInstaller

    expected_pyinstaller = "6.12.0"
    actual_pyinstaller = PyInstaller.__version__
    if actual_pyinstaller != expected_pyinstaller:
        print(
            f"PyInstaller {expected_pyinstaller} is required, found {actual_pyinstaller}",
            file=sys.stderr,
        )
        return 1

    expected_hooks = "2025.2"
    actual_hooks = importlib.metadata.version("pyinstaller-hooks-contrib")
    if actual_hooks != expected_hooks:
        print(
            f"pyinstaller-hooks-contrib {expected_hooks} is required, found {actual_hooks}",
            file=sys.stderr,
        )
        return 1

    if importlib.util.find_spec("pkg_resources") is None:
        print("The pinned PyInstaller toolchain requires pkg_resources", file=sys.stderr)
        return 1

    print(f"PyInstaller: {actual_pyinstaller}")
    print(f"PyInstaller hooks: {actual_hooks}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
