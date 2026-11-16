"""Compatibility entry: forwards arguments and exit status to current acceptance."""

from pathlib import Path
import subprocess
import sys


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    raise SystemExit(subprocess.call(
        [sys.executable, "-B", "-X", "utf8", "-m", "scripts.run_acceptance", *sys.argv[1:]], cwd=root))
