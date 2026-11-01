"""Shared paths and the one helper that imports a file by path.

The container entrypoints carry hyphens -- ``data-validity.py``, ``credit-model.py`` -- because
that is how the reference lab names them, and a hyphen is not a Python identifier. The names are
kept and the cost is paid here, once.
"""

import importlib.util
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
SAMPLE = PROJECT / "data" / "sample"
sys.path.insert(0, str(PROJECT))


def load_module(relative_path, name):
    spec = importlib.util.spec_from_file_location(name, PROJECT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
