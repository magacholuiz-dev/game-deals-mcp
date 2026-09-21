"""Test isolation: no test may touch the real network OR the real database.

THE ENVIRONMENT BELOW MUST BE SET BEFORE ANY game_deals IMPORT. config reads it
once, at import. An earlier version of this file imported game_deals.http first,
which froze DB_PATH at ./deals.db, and a test that empties every table then wiped
the real database on each run. Do not move these lines below the imports.

Offline determinism: no test may touch the real network.

Every test starts with a default HTTP client whose transport raises. A test that
needs HTTP installs its own client built from recorded fixtures (see helpers).
Live probing exists only in scripts/probe.py, never in the test suite.
"""
import hashlib
import os
import tempfile
from pathlib import Path

_SANDBOX = Path(tempfile.mkdtemp(prefix="gamedeals-tests-"))
os.environ["GAMEDEALS_DB"] = str(_SANDBOX / "session.db")
os.environ["HTTP_CACHE_PATH"] = str(_SANDBOX / "http_cache.db")
os.environ["GAMEDEALS_BACKUP_DIR"] = str(_SANDBOX / "backups")

# The files a test run must never change. Recorded before anything runs and
# compared at the end; a difference fails the whole session.
_REPO = Path(__file__).resolve().parents[1]
_GUARDED = [_REPO / "deals.db", _REPO / "deals.db-wal", _REPO / ".http_cache.db"]


def _fingerprint():
    out = {}
    for p in _GUARDED:
        out[p.name] = (hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None)
    return out


_BEFORE = _fingerprint()

import httpx      # noqa: E402
import pytest     # noqa: E402

from game_deals import http   # noqa: E402


def pytest_sessionfinish(session, exitstatus):
    after = _fingerprint()
    changed = [n for n in _BEFORE if _BEFORE[n] != after[n]]
    if changed:
        session.exitstatus = 1
        print(f"\n\n!!! THE TEST RUN MODIFIED REAL FILES: {changed}\n"
              "!!! A test wrote outside its sandbox. Fix that before trusting anything.")


def _blocked(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"network access attempted in a test: {request.url}")


@pytest.fixture(autouse=True)
def _no_network():
    client = http.HttpClient(transport=httpx.MockTransport(_blocked),
                             min_interval=0, cache_path=":memory:",
                             sleep=lambda s: None)
    http.set_default(client)
    yield
    http.set_default(None)


@pytest.fixture()
def scratch_db(tmp_path, monkeypatch):
    """A private database file per test, so tests never share state."""
    from game_deals import config, db
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(db, "_conn", None)
    yield db
    if db._conn is not None:
        db._conn.close()
    monkeypatch.setattr(db, "_conn", None)


class FakeStore:
    """A store provider whose behaviour a test scripts. `answers` maps
    source_id -> list[Offer] (or an Exception to raise, or a str for
    `last_error` with no offers)."""
    kind = "store"

    def __init__(self, name="fake", answers=None):
        self.name, self.label = name, name
        self.answers = answers or {}
        self.last_error = ""
        self.calls = []

    def configured(self):
        return True

    def why_unconfigured(self):
        return ""

    def fetch(self, source_id):
        self.calls.append(source_id)
        a = self.answers.get(source_id, [])
        self.last_error = ""
        if isinstance(a, Exception):
            raise a
        if isinstance(a, str):
            self.last_error = a
            return []
        return a

    def search(self, q, limit=10):
        return []


@pytest.fixture()
def fake_store(monkeypatch):
    """Install FakeStore instances into the provider registry."""
    from game_deals import providers
    installed = {}

    def install(name="fake", answers=None):
        p = FakeStore(name, answers)
        monkeypatch.setitem(providers.ALL, name, p)
        installed[name] = p
        return p

    yield install
