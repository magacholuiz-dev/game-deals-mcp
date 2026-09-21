"""The test suite must be unable to touch a real database. These tests exist
because it once did: see the note at the top of conftest.py."""
import os
from pathlib import Path
import tempfile

import pytest

from game_deals import config, db

REPO = Path(__file__).resolve().parents[1]


def test_the_default_database_for_tests_lives_in_the_temp_directory():
    real = Path(config.DB_PATH).resolve()
    assert Path(tempfile.gettempdir()).resolve() in real.parents
    assert real != (REPO / "deals.db").resolve()


def test_opening_a_real_looking_path_under_pytest_is_refused(monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(REPO / "deals.db"))
    monkeypatch.setattr(db, "_conn", None)
    with pytest.raises(RuntimeError, match="refusing to open"):
        db.conn()
    assert db._conn is None


def test_relative_default_path_is_refused_too(monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", "./deals.db")
    monkeypatch.setattr(db, "_conn", None)
    with pytest.raises(RuntimeError):
        db.conn()


def test_the_http_cache_and_backups_are_sandboxed_as_well():
    tmp = Path(tempfile.gettempdir()).resolve()
    assert tmp in Path(config.HTTP_CACHE_PATH).resolve().parents
    assert tmp in Path(config.BACKUP_DIR).resolve().parents


def test_memory_databases_are_allowed(monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", ":memory:")
    monkeypatch.setattr(db, "_conn", None)
    db.conn()
    monkeypatch.setattr(db, "_conn", None)
