"""Entry point frozen by PyInstaller into bakin-engine.exe (the desktop app's engine)."""
import sys

from bakin.sidecar import main

if __name__ == "__main__":
    sys.exit(main())
