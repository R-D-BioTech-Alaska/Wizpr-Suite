from __future__ import annotations

import importlib
import sys


def main() -> int:
    import numpy as np

    expected = "1.26.4"
    if np.__version__ != expected:
        print(f"NumPy {expected} is required, found {np.__version__}", file=sys.stderr)
        return 1

    exceptions_module = importlib.import_module("numpy.core._exceptions")
    core_module = importlib.import_module("numpy.core._multiarray_umath")

    values = np.arange(8, dtype=np.float32)
    if float(values.sum()) != 28.0:
        print("NumPy compiled core returned an invalid result", file=sys.stderr)
        return 1

    print(f"NumPy: {np.__version__}")
    print(f"NumPy exceptions: {exceptions_module.__file__}")
    print(f"NumPy compiled core: {core_module.__file__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
