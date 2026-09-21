"""Build an HttpClient that serves recorded fixtures instead of the network."""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from game_deals import http

FIXTURES = Path(__file__).parent / "fixtures"


def load(rel: str) -> bytes:
    return (FIXTURES / rel).read_bytes()


def price_router(*fixture_names: str):
    """Answer api.ec.nintendo.com price queries from recorded live fixtures.

    Merges the `prices` of the given fixtures and returns only the requested
    ids. An id that no fixture covers answers 404 (loud), so a test can never
    pass on an invented price."""
    known: dict[str, dict] = {}
    for name in fixture_names:
        for p in json.loads(load(name))["prices"]:
            known[str(p["title_id"])] = p

    def route(request: httpx.Request):
        ids = request.url.params.get("ids", "").split(",")
        missing = [i for i in ids if i not in known]
        if missing:
            return httpx.Response(404, text=f"no recorded fixture for {missing}")
        return httpx.Response(200, json={"personalized": False, "country": "BR",
                                         "prices": [known[i] for i in ids]})
    return route


def fixture_client(routes: dict[str, object], **kw) -> http.HttpClient:
    """`routes` maps a full URL (query included, or just the path part) to a
    fixture path, bytes, str, dict (served as JSON) or an httpx.Response.
    Unknown URLs answer 404, so robots.txt is 'absent' unless a test adds it."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        for key in (url, url.split("?")[0]):
            if key in routes:
                v = routes[key]
                if callable(v):
                    return v(request)
                if isinstance(v, httpx.Response):
                    return v
                if isinstance(v, dict):
                    return httpx.Response(200, json=v)
                if isinstance(v, Path):
                    v = v.read_bytes()
                if isinstance(v, str) and (FIXTURES / v).exists():
                    v = (FIXTURES / v).read_bytes()
                if isinstance(v, str):
                    v = v.encode()
                ctype = ("application/json" if v[:1] in (b"{", b"[")
                         else "text/html; charset=utf-8")
                return httpx.Response(200, content=v,
                                      headers={"content-type": ctype})
        return httpx.Response(404, text="not found")

    kw.setdefault("min_interval", 0)
    kw.setdefault("cache_path", ":memory:")
    kw.setdefault("sleep", lambda s: None)
    client = http.HttpClient(transport=httpx.MockTransport(handler), **kw)
    client.calls = calls                     # type: ignore[attr-defined]
    return client
