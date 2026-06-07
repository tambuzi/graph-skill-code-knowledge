import shutil
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "sample"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    """Keep tests off the real ~/.graphskill (per-project DBs live there now)."""
    monkeypatch.setenv("GRAPHSKILL_HOME", str(tmp_path_factory.mktemp("gshome")))


@pytest.fixture
def sample_repo(tmp_path):
    """A writable copy of the sample fixture repo."""
    dst = tmp_path / "repo"
    shutil.copytree(FIXTURE, dst)
    return dst
