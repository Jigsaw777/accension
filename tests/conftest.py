import shutil
from pathlib import Path
import pytest
from local_ai_router.config import load, Repository
from local_ai_router.engine import Engine

@pytest.fixture
def settings(tmp_path):
    source = Path(__file__).resolve().parents[1]
    shutil.copytree(source/"config", tmp_path/"config", ignore=shutil.ignore_patterns("local.yaml"))
    shutil.copytree(source/"prompts", tmp_path/"prompts")
    s = load(tmp_path, mock=True)
    s.discovery.enabled = False
    return s

@pytest.fixture
def repo(settings, tmp_path):
    root = tmp_path/"repo"
    root.mkdir()
    settings.repositories = [Repository(path=str(root), validation={"tests": ["{python}", "-m", "unittest", "discover", "-v"]})]
    return root

@pytest.fixture
async def engine(settings):
    e = Engine(settings)
    yield e
    await e.close()
