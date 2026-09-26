"""Point the package at a throwaway copy of the repo before it is imported."""

import os
import shutil
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_HOME = Path(tempfile.mkdtemp(prefix="daily_paper_test_"))
shutil.copytree(_REPO / "config", _HOME / "config")
os.environ["DAILY_PAPER_HOME"] = str(_HOME)
