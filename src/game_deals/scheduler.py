"""Persistent scheduler: decides what is due, runs it, and remembers.

The old setup was launchd firing a script at fixed clock times. A laptop that
sleeps simply skipped those runs and nothing recorded the fact, so the history
looked complete when it was not. This engine keeps state in SQLite instead.

Ticks. Each job has a cadence. Time is cut into ticks of that length, shifted by
a small per-job jitter so jobs do not all hit the network at the same second.
A job is due when no run exists for the CURRENT tick. After a sleep, the ticks
that passed are not replayed: one catch-up run answers the latest tick, and the
missed ticks show up as the gap between expected and recorded runs, which is
exactly what the verdict later uses to lower its confidence.

Failure handling. A failed run is retried with exponential backoff, and three
failures in a row open a circuit that skips the job for a growing time, so a
broken site is not hammered every few minutes. One good run closes it.

Nothing here talks to the network itself. It calls the collector.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import alerts, collector, db, events, jobs, providers

HOUR, DAY = 3600, 86400

# Seconds between runs. From the project brief: the eShop and the PS Store twice
# a day, Steam four times, the community feed hourly, ratings weekly, ITAD daily.
CADENCE_S: dict[str, int] = {
    "nintendo": 12 * HOUR, "playstation": 12 * HOUR, "steam": 6 * HOUR,
    "itad": DAY, "promobit": HOUR, "rawg": 7 * DAY, "backup": DAY,
    "mercadolivre": 12 * HOUR, "amazon": 12 * HOUR, "shopee": 12 * HOUR,
}
DEFAULT_CADENCE_S = DAY
NOT_ACCELERATED = {"rawg", "backup"}     # more frequent runs would change nothing

FAILURES_TO_OPEN_CIRCUIT = 3
CIRCUIT_BASE_S, CIRCUIT_MAX_S = 900, 6 * HOUR
RETRY_BASE_S = 300


def cadence_for(source: str, today: dt.date | None = None) -> int:
    """Seconds between runs. Halved from 3 days before a major sale until it
    ends, when prices actually move. Estimated events never trigger it."""
    base = CADENCE_S.get(source, DEFAULT_CADENCE_S)
    if source not in NOT_ACCELERATED and events.near_event(today):
        return max(base // 2, 15 * 60)
    return base


def jitter_for(source: str, cadence_s: int) -> int:
    """Stable offset in [0, min(15 min, cadence/4)) so jobs do not align."""
    span = max(1, min(900, cadence_s // 4))
    return int(hashlib.sha256(source.encode()).hexdigest(), 16) % span


def current_tick(source: str, now: int, cadence_s: int | None = None) -> int:
    c = cadence_s or cadence_for(source, dt.date.fromtimestamp(now))
    j = jitter_for(source, c)
    return ((now - j) // c) * c + j


def retry_delay(consecutive_failures: int, cadence_s: int) -> int:
    return min(cadence_s, RETRY_BASE_S * 2 ** max(0, consecutive_failures - 1))


def circuit_delay(consecutive_failures: int) -> int:
    extra = consecutive_failures - FAILURES_TO_OPEN_CIRCUIT
    return min(CIRCUIT_MAX_S, CIRCUIT_BASE_S * 2 ** max(0, extra))


# ------------------------------------------------------------------- outcomes

@dataclass
class Outcome:
    status: str = jobs.OK
    items: int = 0
    attempted: int = 0
    failed: int = 0
    latency_ms: int | None = None
    error: str = ""
    error_kind: str = ""


@dataclass
class TickReport:
    now: int
    ran: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    not_due: list[str] = field(default_factory=list)
    missed_ticks: dict[str, int] = field(default_factory=dict)
    health_changes: list[tuple[str, str, str]] = field(default_factory=list)
    alerts: int = 0


# --------------------------------------------------------------- job registry

def _other_runner(name: str) -> Callable[[int], Outcome]:
    def rawg(now: int) -> Outcome:
        from . import ratings
        if not ratings.configured():
            return Outcome(jobs.SKIPPED, error="RAWG_API_KEY not set", error_kind="other")
        rows = db.conn().execute(
            "SELECT id, rawg_id FROM products WHERE rawg_id IS NOT NULL").fetchall()
        items = failed = 0
        for r in rows:
            ficha = ratings.refresh(r["rawg_id"])
            if ficha is None:
                failed += 1
                continue
            ratings.salvar(r["id"], ficha)
            items += 1
        status = jobs.OK if not failed else (jobs.FAILED if failed == len(rows) else jobs.PARTIAL)
        return Outcome(status, items, len(rows), failed,
                       error="RAWG refresh failed" if failed else "",
                       error_kind="http" if failed else "")

    def backup(now: int) -> Outcome:
        from . import backup as b
        path = b.run_backup()
        return Outcome(jobs.OK, items=1, attempted=1, error="", error_kind="") \
            if path else Outcome(jobs.FAILED, attempted=1, failed=1,
                                 error="backup failed integrity check", error_kind="other")

    return {"rawg": rawg, "backup": backup}[name]


def job_names() -> dict[str, str]:
    """name -> kind (store | feed | other)."""
    out = {n: "store" for n in providers.lojas()}
    out.update({n: "feed" for n in providers.feeds()})
    out["rawg"] = "other"
    out["backup"] = "other"
    return out


# ----------------------------------------------------------------------- tick

def _due(name: str, now: int, force: bool, report: TickReport) -> int | None:
    """The tick this job should answer now, or None if it should not run."""
    st = jobs.state(name)
    if not force and st["circuit_open_until"] > now:
        report.skipped[name] = ("disjuntor aberto por mais "
                                f"{(st['circuit_open_until'] - now) // 60} min")
        return None
    cadence = cadence_for(name, dt.date.fromtimestamp(now))
    tick = current_tick(name, now, cadence)
    if force:
        return tick
    last = jobs.last_run(name, statuses=(jobs.OK, jobs.PARTIAL, jobs.FAILED))
    if last is not None and (last["scheduled_for"] or 0) >= tick:
        if last["status"] in (jobs.OK, jobs.PARTIAL):
            report.not_due.append(name)
            return None
        wait = retry_delay(st["consecutive_failures"], cadence)
        if now < (last["finished_at"] or last["started_at"]) + wait:
            report.not_due.append(name)
            return None
    # Catch-up bookkeeping: how many ticks went by since the last good run.
    ok = jobs.last_run(name, statuses=(jobs.OK, jobs.PARTIAL))
    if ok is not None and ok["scheduled_for"]:
        missed = (tick - ok["scheduled_for"]) // cadence - 1
        if missed > 0:
            report.missed_ticks[name] = missed
    return tick


def _record_outcome(name: str, out: Outcome, now: int) -> None:
    st = jobs.state(name)
    if out.status == jobs.FAILED:
        n = st["consecutive_failures"] + 1
        opens = now + circuit_delay(n) if n >= FAILURES_TO_OPEN_CIRCUIT else 0
        jobs.set_state(name, consecutive_failures=n, circuit_open_until=opens)
    elif out.status in (jobs.OK, jobs.PARTIAL):
        jobs.set_state(name, consecutive_failures=0, circuit_open_until=0)


def _announce(report: TickReport) -> None:
    """Tell the user when a source BREAKS or comes back, once per transition."""
    from . import health
    for h in health.check_source_health(report.now):
        if h.status in (health.INACTIVE, health.UNKNOWN):
            continue
        old = jobs.state(h.source)["last_health"]
        if h.status == old:
            continue
        jobs.set_state(h.source, last_health=h.status)
        report.health_changes.append((h.source, old or "-", h.status))
        if h.status == health.BROKEN:
            alerts.notify(f"Fonte quebrou: {h.source}", "; ".join(h.reasons[:2]))
        elif old == health.BROKEN:
            # Leaving "broken" is the news. It usually lands on "degraded" first:
            # recent failures stay in the lookback window for a while.
            alerts.notify(f"Fonte voltou: {h.source}",
                          "coletando de novo" + ("" if h.status == health.HEALTHY
                                                 else f" (ainda {health.LABEL[h.status]})"))


def tick(now: int | None = None, *, sources: list[str] | None = None,
         force: bool = False, verbose: bool = False) -> TickReport:
    """Run everything that is due. Safe to call as often as you like."""
    now = now or db.now()
    report = TickReport(now)
    jobs.abandon_stale(now=now)
    names = job_names()
    if sources:
        names = {n: k for n, k in names.items() if n in sources}

    due: dict[str, int] = {}
    for name in names:
        t = _due(name, now, force, report)
        if t is not None:
            due[name] = t

    store_due = {n: t for n, t in due.items() if names[n] == "store"}
    if store_due:
        runs = collector.collect(list(store_due), now=now, scheduled_for=store_due,
                                 verbose=verbose)
        for name, r in runs.items():
            _record_outcome(name, Outcome(r.status, r.items, r.attempted, r.failed,
                                          r.latency_ms, r.error, r.error_kind), now)
            report.ran.append(name)
            report.alerts += len(r.alerts)

    for name, t in due.items():
        kind = names[name]
        if kind == "feed":
            r = collector.collect_feed(name, now=now, scheduled_for=t, verbose=verbose)
            _record_outcome(name, Outcome(r.status, r.items, r.attempted, r.failed,
                                          r.latency_ms, r.error, r.error_kind), now)
            report.ran.append(name)
            report.alerts += len(r.alerts)
        elif kind == "other":
            run_id = jobs.start(name, t, kind="job", now=now)
            t0 = time.perf_counter()
            try:
                out = _other_runner(name)(now)
            except Exception as e:                        # noqa: BLE001
                out = Outcome(jobs.FAILED, attempted=1, failed=1,
                              error=f"{type(e).__name__}: {e}",
                              error_kind=jobs.classify_error("", e))
            out.latency_ms = int((time.perf_counter() - t0) * 1000)
            jobs.finish(run_id, out.status, items=out.items, attempted=out.attempted,
                        failed=out.failed, latency_ms=out.latency_ms, error=out.error,
                        error_kind=out.error_kind, now=now)
            _record_outcome(name, out, now)
            report.ran.append(name)

    _announce(report)
    return report


def serve(poll_s: int = 60, *, sleep: Callable[[float], None] = time.sleep,
          stop: Callable[[], bool] = lambda: False, verbose: bool = False) -> None:
    """Long-running loop. If the machine sleeps, the next iteration simply finds
    a later `now` and runs the catch-up: no replay, the gap stays on record."""
    while not stop():
        try:
            rep = tick(verbose=verbose)
            if verbose and (rep.ran or rep.health_changes):
                print(f"[{dt.datetime.now():%H:%M:%S}] ran={rep.ran} "
                      f"changes={rep.health_changes}", flush=True)
        except Exception as e:                            # noqa: BLE001
            print(f"scheduler tick failed: {type(e).__name__}: {e}", flush=True)
        sleep(poll_s)


def main() -> None:
    import argparse
    from . import health
    ap = argparse.ArgumentParser(description="game-deals scheduler")
    ap.add_argument("--once", action="store_true", help="run what is due, then exit")
    ap.add_argument("--serve", action="store_true", help="loop forever")
    ap.add_argument("--force", action="store_true", help="ignore ticks and circuits")
    ap.add_argument("--sources", default="", help="comma separated job names")
    ap.add_argument("--status", action="store_true", help="show health, run nothing")
    ap.add_argument("--poll", type=int, default=60)
    a = ap.parse_args()
    src = [s for s in a.sources.split(",") if s] or None

    if a.status:
        summary = health.summarize(health.check_source_health())
        print(f"geral: {summary['overall_label']}")
        for s in summary["sources"]:
            why = f"  ({'; '.join(s['reasons'][:2])})" if s["reasons"] else ""
            print(f"  {s['label']:11} {s['source']:14} {s['kind']:6}{why}")
        return
    if a.serve:
        serve(a.poll, verbose=True)
        return
    rep = tick(sources=src, force=a.force, verbose=True)
    print(f"ran={rep.ran} not_due={rep.not_due} skipped={rep.skipped} "
          f"missed={rep.missed_ticks} changes={rep.health_changes}")


if __name__ == "__main__":
    main()
