import shutil
from pathlib import Path

import pytest

from local_ai_router.config import Repository, load
from local_ai_router.engine import Engine


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    """Provider tests must use mocks; allow local servers and event-loop sockets."""
    import socket

    original = socket.socket.connect
    original_ex = socket.socket.connect_ex

    def check(address):
        if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1", "localhost"}:
            raise AssertionError("Unexpected external network attempt in offline tests")

    def connect(sock, address):
        check(address)
        return original(sock, address)

    def connect_ex(sock, address):
        check(address)
        return original_ex(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)


@pytest.fixture
def settings(tmp_path):
    source = Path(__file__).resolve().parents[1]
    shutil.copytree(source / "config", tmp_path / "config", ignore=shutil.ignore_patterns("local.yaml"))
    shutil.copytree(source / "prompts", tmp_path / "prompts")
    s = load(tmp_path, mock=True)
    s.discovery.enabled = False
    return s


@pytest.fixture
def repo(settings, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    settings.repositories = [
        Repository(path=str(root), validation={"tests": ["{python}", "-m", "unittest", "discover", "-v"]})
    ]
    return root


@pytest.fixture
async def engine(settings):
    e = Engine(settings)
    yield e
    await e.close()
