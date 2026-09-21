import httpx
import pytest

from game_deals import http
from tests.helpers import fixture_client

PAGE = "https://example.test/page"
ROBOTS_CLOSED = "User-agent: *\nDisallow: /private\nDisallow: /wild*\n"


def test_rate_limit_is_one_request_per_second_per_host():
    now = [0.0]
    slept: list[float] = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    c = fixture_client({"https://a.test/x": "ok", "https://a.test/y": "ok",
                        "https://b.test/x": "ok"},
                       min_interval=1.0, clock=lambda: now[0], sleep=sleep)
    c.get("https://a.test/x", robots=False)
    c.get("https://a.test/y", robots=False)       # same host, no time passed
    assert slept and abs(slept[-1] - 1.0) < 1e-9
    before = len(slept)
    c.get("https://b.test/x", robots=False)       # other host: no wait
    assert len(slept) == before


def test_robots_blocks_and_raises():
    c = fixture_client({"https://example.test/robots.txt": ROBOTS_CLOSED,
                        "https://example.test/private/a": "secret"})
    with pytest.raises(http.RobotsBlocked):
        c.get("https://example.test/private/a")
    with pytest.raises(http.RobotsBlocked):
        c.get("https://example.test/wildcard/x")      # wildcard rule
    assert not any("/private/a" in u for u in c.calls)   # never even requested


def test_robots_allows_open_paths_and_is_fetched_once():
    c = fixture_client({"https://example.test/robots.txt": ROBOTS_CLOSED,
                        PAGE: "hello"})
    assert c.get(PAGE).text == "hello"
    c.get(PAGE)
    robots_calls = [u for u in c.calls if u.endswith("/robots.txt")]
    assert len(robots_calls) == 1


def test_missing_robots_allows_but_server_error_denies():
    ok = fixture_client({PAGE: "hi"})             # robots.txt -> 404 -> allow
    assert ok.get(PAGE).text == "hi"

    down = fixture_client({"https://example.test/robots.txt":
                           httpx.Response(503), PAGE: "hi"})
    with pytest.raises(http.RobotsBlocked):
        down.get(PAGE)


def test_etag_revalidation_uses_cached_body_on_304():
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, content=b"body-v1",
                              headers={"etag": '"v1"',
                                       "content-type": "text/plain"})

    c = http.HttpClient(transport=httpx.MockTransport(handler), min_interval=0,
                        cache_path=":memory:", sleep=lambda s: None)
    first = c.get(PAGE)
    second = c.get(PAGE)
    assert first.text == second.text == "body-v1"
    assert getattr(second, "from_cache", False) is True


def test_ttl_avoids_the_network_entirely():
    c = fixture_client({PAGE: "sitemap"})
    c.get(PAGE, ttl=3600)
    n = len(c.calls)
    r = c.get(PAGE, ttl=3600)
    assert r.text == "sitemap" and len(c.calls) == n
    assert r.from_cache is True


def test_offline_guard_fires_for_the_default_client():
    """conftest installs a blocking default client: a test that forgets to
    provide fixtures must fail loudly instead of hitting the network."""
    with pytest.raises(AssertionError, match="network access attempted"):
        http.get("https://example.test/anything", robots=False)
