"""Pytest fixtures: isolated temp DB + FastAPI TestClient."""

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_tmp_dir = tempfile.TemporaryDirectory()
_db_path = Path(_tmp_dir.name) / "test.db"
os.environ["RULES_DB_PATH"] = str(_db_path)

from app import store  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _database():
    store.init_db(_db_path)
    yield
    _tmp_dir.cleanup()


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def conn():
    connection = store.connect(_db_path)
    yield connection
    connection.close()
