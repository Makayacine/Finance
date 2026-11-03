"""Shared setup: the project root on the path, FIA's import-time noise kept out of the way.

``FIA.py`` is vendored unchanged. It prints a line when matplotlib is missing and compiles a
string comparison with ``is not``, which Python reports as a SyntaxWarning; neither is a
failure, so the second is filtered here.
"""

import sys
import warnings
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

warnings.filterwarnings("ignore", category=SyntaxWarning)
