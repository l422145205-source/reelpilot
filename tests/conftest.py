from pathlib import Path
import os
import sys
import uuid
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"src"))
if (ROOT/".runtime/site").exists():
    sys.path.insert(0, str(ROOT/".runtime/site"))

from reelpilot.library import Library, save_library
from reelpilot.schema import Asset, Segment


# Opt-in only: Python 3.13 private temporary-directory ACLs can exclude the
# Windows desktop sandbox identity. This changes test scratch-directory creation,
# never application logic or system permissions. Normal local/CI pytest is unchanged.
if sys.platform == "win32" and os.environ.get("REELPILOT_TEST_TMP"):
    class SandboxScratch:
        def mktemp(self, basename, numbered=True):
            root = Path(os.environ["REELPILOT_TEST_TMP"]).resolve()
            if not root.is_relative_to(ROOT.resolve()):
                raise ValueError("test scratch root must be inside this checkout")
            path = root/(basename+"-"+uuid.uuid4().hex)
            path.mkdir(mode=0o755, parents=True)
            return path

    @pytest.fixture(scope="session")
    def tmp_path_factory():
        return SandboxScratch()

    @pytest.fixture
    def tmp_path(tmp_path_factory):
        return tmp_path_factory.mktemp("case")


@pytest.fixture
def library(tmp_path):
    asset = Asset(id="film", path="media/film.mp4", duration=60, sha256="a"*64,
                  title="Test metadata", attribution="Original test data", license="MIT")
    rows = [Segment(id="forest", asset_id="film", start=0, end=6, transcript="green forest path"),
            Segment(id="robot", asset_id="film", start=12, end=18, transcript="robot city street"),
            Segment(id="dragon", asset_id="film", start=24, end=30, transcript="red dragon mountain")]
    save_library(tmp_path/"collection", [asset], rows)
    return Library(tmp_path/"collection")

