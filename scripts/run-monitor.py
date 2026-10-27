#!/usr/bin/env python3
"""Scheduler entry point: use absolute paths without installing a Python package."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from arcteryx_prices.__main__ import main

if __name__ == '__main__':
    main()
