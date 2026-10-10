#!/usr/bin/python3
"""Relocatable entry point; works when the unpacked directory contains spaces."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from ice_circuit.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
