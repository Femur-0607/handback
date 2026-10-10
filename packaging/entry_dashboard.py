"""PyInstaller entry point for handback-dashboard.exe (windowed)."""
import sys

from handback.dashboard import main

if __name__ == "__main__":
    sys.exit(main())
