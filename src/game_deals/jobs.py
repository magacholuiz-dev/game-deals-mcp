"""Persistence for scheduled work: what ran, when, and how it went."""
from __future__ import annotations

import time
from typing import Any

from . import db

OK, PARTIAL, FAILED, SKIPPED, RUNNING = "ok", "partial", "failed", "skipped", "running"
FINISHED = (OK, PARTIAL, FAILED, SKIPPED)


def start(source: str, scheduled_for: int | None = None, kind: str = "collect",
          now: int | None = None) -> int:
    cur = db.conn().execute(
        "INSERT INTO job_runs(source,kind,scheduled_for,started_at,status) "
        "VALUES (?,?,?,?,?)", (source, kind, scheduled_for, now or db.now(), RUNNING))
    db.conn().commit()
    return int(cur.lastrowid)


def finish(run_id: int, status: str, *, items: int = 0, attempted: int = 0,
           failed: int = 0, latency_ms: int | None = None, error: str = "",
           error_kind: str = "", now: int | None = None) -> None:
    db.conn().execute(
        "UPDATE job_runs SET finished_at=?, status=?, items=?, attempted=?, failed=?,"
        " latency_ms=?, error=?, error_kind=? WHERE id=?",
        (now or db.now(), status, items, attempted, failed, latency_ms,
         error[:500], error_kind, run_id))
    db.conn().commit()


def record(source: str, status: str, *, scheduled_for: int | None = None,
           started_at: int | None = None, finished_at: int | None = None,
           items: int = 0, attempted: int = 0, failed: int = 0,
           latency_ms: int | None = None, error: str = "", error_kind: str = "",
           kind: str = "collect") -> int:
    """Insert a finished run in one call (used by tests and by imports)."""
    t0 = started_at if started_at is not None else db.now()
    cur = db.conn().execute(
        "INSERT INTO job_runs(source,kind,scheduled_for,started_at,finished_at,"
        "status,items,attempted,failed,latency_ms,error,error_kind) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (source, kind, scheduled_for, t0, finished_at if finished_at is not None else t0,
         status, items, attempted, failed, latency_ms, error[:500], error_kind))
    db.conn().commit()
    return int(cur.lastrowid)


def runs(source: str | None = None, since: int | None = None,
         until: int | None = None, statuses: tuple[str, ...] | None = None,
         limit: int | None = None, newest_first: bool = False) -> list[Any]:
    sql, args = "SELECT * FROM job_runs WHERE 1=1", []
    if source:
        sql += " AND source=?"
        args.append(source)
    if since is not None:
        sql += " AND started_at>=?"
        args.append(since)
    if until is not None:
        sql += " AND started_at<=?"
        args.append(until)
    if statuses:
        sql += f" AND status IN ({','.join('?' * len(statuses))})"
        args += list(statuses)
    sql += " ORDER BY started_at " + ("DESC" if newest_first else "ASC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return db.conn().execute(sql, args).fetchall()


def first_run(source: str):
    return db.conn().execute(
        "SELECT * FROM job_runs WHERE source=? AND status<>? "
        "ORDER BY started_at LIMIT 1", (source, RUNNING)).fetchone()


def last_run(source: str, statuses: tuple[str, ...] = FINISHED):
    q = ",".join("?" * len(statuses))
    return db.conn().execute(
        f"SELECT * FROM job_runs WHERE source=? AND status IN ({q}) "
        "ORDER BY started_at DESC LIMIT 1", (source, *statuses)).fetchone()


def sources_with_runs() -> list[str]:
    return [r["source"] for r in db.conn().execute(
        "SELECT DISTINCT source FROM job_runs ORDER BY source")]


# ------------------------------------------------------------------- state

def state(source: str) -> dict[str, Any]:
    r = db.conn().execute("SELECT * FROM job_state WHERE source=?", (source,)).fetchone()
    return dict(r) if r else {"source": source, "consecutive_failures": 0,
                              "circuit_open_until": 0, "last_health": ""}


def set_state(source: str, **fields: Any) -> None:
    cur = state(source)
    cur.update(fields)
    db.conn().execute(
        "INSERT INTO job_state(source,consecutive_failures,circuit_open_until,"
        "last_health) VALUES (?,?,?,?) ON CONFLICT(source) DO UPDATE SET "
        "consecutive_failures=excluded.consecutive_failures,"
        "circuit_open_until=excluded.circuit_open_until,"
        "last_health=excluded.last_health",
        (source, cur["consecutive_failures"], cur["circuit_open_until"],
         cur["last_health"]))
    db.conn().commit()


def abandon_stale(older_than_s: int = 3600, now: int | None = None) -> int:
    """A run left in 'running' (the process died or the Mac slept through it)
    would otherwise stay there forever and hide as if it were still working."""
    cutoff = (now or db.now()) - older_than_s
    cur = db.conn().execute(
        "UPDATE job_runs SET status=?, finished_at=?, error=?, error_kind=? "
        "WHERE status=? AND started_at<?",
        (FAILED, now or db.now(), "run never finished (process ended or machine slept)",
         "other", RUNNING, cutoff))
    db.conn().commit()
    return cur.rowcount


# ------------------------------------------------------------ error kinds

# What counts against a SOURCE. A game that is simply not sold in Brazil, or an
# ambiguous edition, is a fact about the product, not a fault of the store, and
# must not make the source look broken.
FAILURE_KINDS = ("robots", "timeout", "http", "drift", "other")
DATA_KINDS = ("not_found", "ambiguous")

_DRIFT = ("format changed", "no product state", "no offers", "no json-ld",
          "no nsuid", "no products in the html")
_NOT_FOUND = ("does not price in", "not sold", "no purchasable", "no '",
              "is not sold here", "answered 404")
_AMBIGUOUS = ("several editions",)


def classify_error(text: str = "", exc: BaseException | None = None) -> str:
    """Bucket an error into robots | timeout | http | drift | other | not_found |
    ambiguous, or '' when there is none."""
    if exc is not None:
        import httpx                       # local: jobs.py stays import-light
        from . import http as _http
        if isinstance(exc, _http.RobotsBlocked):
            return "robots"
        if isinstance(exc, httpx.TimeoutException):
            return "timeout"
        if isinstance(exc, httpx.HTTPError):
            return "http"
        if isinstance(exc, (ValueError, KeyError, TypeError, AttributeError,
                            IndexError)):
            return "drift"              # a parser blew up on unexpected data
        return "other"
    t = text.lower()
    if not t:
        return ""
    if "robots.txt forbids" in t:
        return "robots"
    if "timed out" in t or "timeout" in t:
        return "timeout"
    if any(m in t for m in _AMBIGUOUS):
        return "ambiguous"
    if any(m in t for m in _DRIFT):
        return "drift"
    if any(m in t for m in _NOT_FOUND):
        return "not_found"
    if "answered 5" in t or "answered 4" in t or "connection" in t:
        return "http"
    return "other"
