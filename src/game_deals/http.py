"""The only way providers should touch the network.

Enforces, in one place, the rules the project promises:

- at most one request per second per host (`min_interval`);
- robots.txt is read and obeyed, including wildcard rules (see robots.py);
- an honest User-Agent;
- conditional GETs with ETag / Last-Modified, plus an optional TTL that avoids
  the network entirely for slow-moving resources such as sitemaps.

Nothing here tries to defeat a block. A refusal from robots.txt raises
`RobotsBlocked`; a provider is expected to treat that as "source unavailable"
and the health check will show why.
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit

import httpx

from . import config
from .robots import Robots


class RobotsBlocked(Exception):
    """robots.txt forbids this URL for our agent."""

    def __init__(self, url: str):
        super().__init__(f"robots.txt forbids automated access to {url}")
        self.url = url


class HttpClient:
    def __init__(self, *, user_agent: str | None = None,
                 min_interval: float | None = None,
                 cache_path: str | None = None,
                 transport: httpx.BaseTransport | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 timeout: float | None = None,
                 robots_ttl: float = 86400.0):
        self.user_agent = user_agent or config.USER_AGENT
        self.min_interval = (config.HTTP_MIN_INTERVAL
                             if min_interval is None else min_interval)
        self._clock, self._sleep = clock, sleep
        self._robots_ttl = robots_ttl
        self._client = httpx.Client(
            transport=transport, timeout=timeout or config.HTTP_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": self.user_agent, "Accept": "*/*"})
        self._last: dict[str, float] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._meta_lock = threading.Lock()
        self._robots: dict[str, tuple[Robots, float]] = {}
        self._db = sqlite3.connect(cache_path or config.HTTP_CACHE_PATH,
                                   check_same_thread=False)
        self._db_lock = threading.Lock()
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS http_cache("
            "key TEXT PRIMARY KEY, url TEXT, etag TEXT, last_modified TEXT,"
            " content_type TEXT, body BLOB, fetched_at REAL)")
        self.requests_made = 0          # network requests, robots.txt included

    # ---------------------------------------------------------------- throttle

    def _host_lock(self, host: str) -> threading.Lock:
        with self._meta_lock:
            return self._locks.setdefault(host, threading.Lock())

    def _send(self, method: str, url: str, **kw: Any) -> httpx.Response:
        host = urlsplit(url).netloc
        with self._host_lock(host):
            wait = self._last.get(host, -1e9) + self.min_interval - self._clock()
            if wait > 0:
                self._sleep(wait)
            try:
                return self._client.request(method, url, **kw)
            finally:
                self._last[host] = self._clock()
                self.requests_made += 1

    # ------------------------------------------------------------------ robots

    def _robots_for(self, url: str) -> Robots:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        cached = self._robots.get(origin)
        if cached and self._clock() - cached[1] < self._robots_ttl:
            return cached[0]
        try:
            r = self._send("GET", origin + "/robots.txt")
            robots = (Robots.parse(r.text) if r.status_code == 200
                      else Robots.unavailable(r.status_code))
        except httpx.HTTPError:
            robots = Robots.unavailable(None)
        # A failed fetch is retried soon instead of being remembered for a day.
        stamp = self._clock() if not robots.deny_all else \
            self._clock() - self._robots_ttl + 300
        self._robots[origin] = (robots, stamp)
        return robots

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        return self._robots_for(url).allowed(self.user_agent, path or "/")

    # ------------------------------------------------------------------- cache

    @staticmethod
    def _key(url: str, params: dict | None) -> str:
        q = urlencode(sorted((params or {}).items()))
        return hashlib.sha256(f"{url}?{q}".encode()).hexdigest()

    def _cache_get(self, key: str):
        with self._db_lock:
            return self._db.execute(
                "SELECT etag,last_modified,content_type,body,fetched_at "
                "FROM http_cache WHERE key=?", (key,)).fetchone()

    def _cache_put(self, key, url, etag, last_mod, ctype, body) -> None:
        with self._db_lock:
            self._db.execute(
                "INSERT OR REPLACE INTO http_cache VALUES (?,?,?,?,?,?,?)",
                (key, url, etag, last_mod, ctype, body, time.time()))
            self._db.commit()

    def _cache_touch(self, key: str) -> None:
        with self._db_lock:
            self._db.execute("UPDATE http_cache SET fetched_at=? WHERE key=?",
                             (time.time(), key))
            self._db.commit()

    @staticmethod
    def _from_cache(url: str, row) -> httpx.Response:
        etag, last_mod, ctype, body, _ = row
        resp = httpx.Response(200, content=body, headers={
            "content-type": ctype or "application/octet-stream"},
            request=httpx.Request("GET", url))
        resp.from_cache = True                      # type: ignore[attr-defined]
        return resp

    # -------------------------------------------------------------------- API

    def get(self, url: str, *, params: dict | None = None,
            headers: dict | None = None, ttl: float = 0.0,
            robots: bool = True) -> httpx.Response:
        full = url + (("?" + urlencode(params)) if params else "")
        if robots and not self.allowed(full):
            raise RobotsBlocked(full)

        key = self._key(url, params)
        row = self._cache_get(key)
        if row and ttl > 0 and time.time() - row[4] < ttl:
            return self._from_cache(url, row)

        hdrs = dict(headers or {})
        if row:
            if row[0]:
                hdrs["If-None-Match"] = row[0]
            if row[1]:
                hdrs["If-Modified-Since"] = row[1]
        resp = self._send("GET", url, params=params, headers=hdrs)

        if resp.status_code == 304 and row:
            self._cache_touch(key)
            return self._from_cache(url, row)
        if resp.status_code == 200:
            etag = resp.headers.get("etag")
            last_mod = resp.headers.get("last-modified")
            if etag or last_mod or ttl > 0:
                self._cache_put(key, url, etag, last_mod,
                                resp.headers.get("content-type"), resp.content)
        return resp

    def request(self, method: str, url: str, *, robots: bool = True,
                **kw: Any) -> httpx.Response:
        """Uncached request (POST and friends). Still throttled and robots-aware."""
        if robots and not self.allowed(url):
            raise RobotsBlocked(url)
        return self._send(method, url, **kw)

    def close(self) -> None:
        self._client.close()
        self._db.close()


# ------------------------------------------------------------ module default

_default: HttpClient | None = None


def default() -> HttpClient:
    global _default
    if _default is None:
        _default = HttpClient()
    return _default


def set_default(client: HttpClient | None) -> None:
    global _default
    _default = client


def get(url: str, **kw: Any) -> httpx.Response:
    return default().get(url, **kw)


def request(method: str, url: str, **kw: Any) -> httpx.Response:
    return default().request(method, url, **kw)
