import struct
import sys


def main() -> int:
    if sys.version_info[:2] != (3, 11):
        print(f"Python 3.11 is required, found {sys.version}", file=sys.stderr)
        return 1
    if struct.calcsize("P") * 8 != 64:
        print("A 64-bit Python build is required", file=sys.stderr)
        return 1
    print(sys.version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
