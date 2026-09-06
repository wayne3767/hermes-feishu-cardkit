"""Test harness: make the Hermes Agent checkout importable and load this plugin directory as the
``feishu_cardkit`` package (the same way Hermes's plugin loader imports it at runtime).

Set ``HERMES_AGENT_ROOT`` to a Hermes checkout; default ``~/.hermes/hermes-agent``.  Run with a
Python that has Hermes's dependencies (its venv, or ``.venv`` created with a ``.pth`` pointing at it).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
HERMES_ROOT = Path(os.environ.get("HERMES_AGENT_ROOT") or Path.home() / ".hermes" / "hermes-agent").resolve()

if not (HERMES_ROOT / "gateway").is_dir():
    raise RuntimeError(f"Hermes Agent checkout not found at {HERMES_ROOT}; set HERMES_AGENT_ROOT")
if str(HERMES_ROOT) not in sys.path:
    sys.path.insert(0, str(HERMES_ROOT))
os.chdir(HERMES_ROOT)  # Hermes resolves some paths relative to its root

if "feishu_cardkit" not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        "feishu_cardkit", PLUGIN_ROOT / "__init__.py", submodule_search_locations=[str(PLUGIN_ROOT)])
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "feishu_cardkit"
    sys.modules["feishu_cardkit"] = module
    spec.loader.exec_module(module)
