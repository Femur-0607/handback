"""Compatibility entry point: python handback.py <command>."""
from handback.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
