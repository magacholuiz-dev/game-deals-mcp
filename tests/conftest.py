"""Offline determinism: no test may touch the real network.

Every test starts with a default HTTP client whose transport raises. A test that
needs HTTP installs its own client built from recorded fixtures (see helpers).
Live probing exists only in scripts/probe.py, never in the test suite.
"""
import httpx
import pytest

from game_deals import http


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
