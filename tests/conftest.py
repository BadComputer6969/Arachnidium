"""Shared test setup: stub tkinter (unavailable headless) and import addon."""

import os
import sys
import types

# addon.py imports tkinter at module level; stub it so the module imports
# headless (attribute access only happens inside start_gui, never in tests).
_tk = types.ModuleType("tkinter")
_ttk = types.ModuleType("tkinter.ttk")
sys.modules.setdefault("tkinter", _tk)
sys.modules.setdefault("tkinter.ttk", _ttk)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO_ROOT)
sys.path.insert(0, REPO_ROOT)
