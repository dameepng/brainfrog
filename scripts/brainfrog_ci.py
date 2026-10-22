#!/usr/bin/env python3
"""Entry point for the trusted BrainFrog CI gate (Phase 1E).

Run it with `python -I trusted/scripts/brainfrog_ci.py <command>` from the trusted checkout. Isolated mode
keeps the working directory and PYTHONPATH off sys.path; only the trusted checkout root is added, so
candidate source is never importable by this process.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.ci_gate import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
