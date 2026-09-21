"""Build an HttpClient that serves recorded fixtures instead of the network."""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from game_deals import http

FIXTURES = Path(__file__).parent / "fixtures"


def load(rel: str) -> bytes:
    return (FIXTURES / rel).read_bytes()


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
