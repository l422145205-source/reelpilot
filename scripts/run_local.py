"""Convenience entry point for a source checkout; contains no machine paths."""
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root/"src"))
if (root/".runtime/site").exists():
    sys.path.insert(0, str(root/".runtime/site"))

from reelpilot.cli import main
main()

