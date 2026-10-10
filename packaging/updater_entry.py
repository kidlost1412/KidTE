"""PyInstaller entry point for the isolated Kid TE launcher."""
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "backend" / "src"))

from update_launcher import main

if __name__ == "__main__":
    raise SystemExit(main())
