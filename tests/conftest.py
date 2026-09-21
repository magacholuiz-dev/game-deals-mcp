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
