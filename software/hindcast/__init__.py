"""Hindcast: scenario studio and settings optimizer on top of the AAPS what-if emulator (docs/hindcast/DESIGN.md)."""
import os as _os
import sys as _sys

# The emulator scripts (emulator_core, emulator_optimize, ns_*) import each other as plain siblings of this
# package's parent directory software/, so importing any hindcast module must make them importable too, from any
# cwd. emulator_core refuses to run unless PYTHONUTF8 is exactly '1' (CLAUDE.md), so it is defaulted here as well.
SOFTWARE_DIR = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if SOFTWARE_DIR not in _sys.path:
    _sys.path.insert(0, SOFTWARE_DIR)
_os.environ.setdefault('PYTHONUTF8', '1')
