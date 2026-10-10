"""PyInstaller entry point for handback.exe (console)."""
import sys

from handback.cli import main

if __name__ == "__main__":
    sys.exit(main())
