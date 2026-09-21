"""Is each source still working? Answered from what job_runs recorded.

A source that breaks silently is the worst failure this project can have: the
dashboard keeps showing old prices as if they were fresh. So health is judged
from the runs themselves, with rules that can be read and tested:

    broken    the circuit breaker is open, or 3 runs failed in a row, or the
              last two runs both hit a format-drift error, or robots.txt now
              forbids us, or the source used to return items and the last 3 runs
              returned none, or there has been no good run for 4 cadences.
    degraded  the last run was partial or failed, more than 20% of recent runs
              failed, latency is high, the source is a bit stale (2 to 4
              cadences), or it has never returned an item.
    healthy   none of the above.
    unknown   nothing has run yet.      inactive   not configured.
"""
from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from typing import Any

from . import db, jobs, providers

HEALTHY, DEGRADED, BROKEN, UNKNOWN, INACTIVE = (
    "healthy", "degraded", "broken", "unknown", "inactive")

CONSECUTIVE_FAILURES_BROKEN = 3
CONSECUTIVE_ZERO_BROKEN = 3
DRIFT_RUNS_BROKEN = 2
FAILURE_RATE_DEGRADED = 0.20
LATENCY_DEGRADED_MS = 8000
STALE_DEGRADED = 2          # cadences without a good run
STALE_BROKEN = 4
LOOKBACK_RUNS = 10
WINDOW_S = 14 * 86400

LABEL = {HEALTHY: "saudável", DEGRADED: "degradada", BROKEN: "quebrada",
         UNKNOWN: "sem dados", INACTIVE: "inativa"}


@dataclass
class SourceHealth:
    source: str
    status: str
    reasons: list[str] = field(default_factory=list)
    kind: str = ""
    cadence_s: int | None = None
    runs_considered: int = 0
    last_run_at: int | None = None
    last_ok_at: int | None = None
    consecutive_failures: int = 0
    consecutive_zero_items: int = 0
    failure_rate: float = 0.0
    p50_latency_ms: int | None = None
    circuit_open_until: int = 0

    def dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["label"] = LABEL[self.status]
        return d


def _trailing(rows: list[Any], pred) -> int:
    n = 0
    for r in rows:                      # newest first
        if not pred(r):
            break
        n += 1
    return n


def _fmt_age(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} dias"


def assess(source: str, now: int, cadence_s: int | None, kind: str = "") -> SourceHealth:
    h = SourceHealth(source, HEALTHY, kind=kind, cadence_s=cadence_s)
    st = jobs.state(source)
    h.circuit_open_until = st["circuit_open_until"]

    rows = [r for r in jobs.runs(source, since=now - WINDOW_S, newest_first=True,
                                 statuses=(jobs.OK, jobs.PARTIAL, jobs.FAILED))]
    rows = rows[:LOOKBACK_RUNS]
    h.runs_considered = len(rows)
    if not rows:
        h.status = UNKNOWN
        h.reasons.append("ainda não houve nenhuma execução")
        return h

    h.last_run_at = rows[0]["started_at"]
    good = [r for r in rows if r["status"] in (jobs.OK, jobs.PARTIAL)]
    h.last_ok_at = good[0]["finished_at"] or good[0]["started_at"] if good else None
    h.consecutive_failures = _trailing(rows, lambda r: r["status"] == jobs.FAILED)
    h.consecutive_zero_items = _trailing(
        rows, lambda r: r["items"] == 0 and r["attempted"] > 0)
    h.failure_rate = round(sum(1 for r in rows if r["status"] == jobs.FAILED) / len(rows), 2)
    lats = [r["latency_ms"] for r in rows if r["latency_ms"] is not None]
    h.p50_latency_ms = int(statistics.median(lats)) if lats else None
    had_items = any(r["items"] > 0 for r in rows)
    last = rows[0]

    broken: list[str] = []
    degraded: list[str] = []

    if h.circuit_open_until > now:
        broken.append(f"disjuntor aberto por mais {_fmt_age(h.circuit_open_until - now)}")
    if h.consecutive_failures >= CONSECUTIVE_FAILURES_BROKEN:
        broken.append(f"{h.consecutive_failures} execuções seguidas falharam")
    drifts = _trailing(rows, lambda r: r["error_kind"] == "drift")
    if drifts >= DRIFT_RUNS_BROKEN:
        broken.append("o formato da resposta mudou e o parser não lê mais "
                      f"({drifts} execuções seguidas)")
    elif drifts == 1:
        degraded.append("a última execução não conseguiu ler a resposta (formato mudou?)")
    if last["error_kind"] == "robots":
        broken.append("o robots.txt do site passou a proibir o acesso")
    if h.consecutive_zero_items >= CONSECUTIVE_ZERO_BROKEN:
        if had_items:
            broken.append(f"passou a não trazer itens: {h.consecutive_zero_items} "
                          "execuções seguidas sem nada")
        else:
            degraded.append("nunca trouxe itens (produtos ainda não encontrados na loja?)")

    if cadence_s:
        age = now - (h.last_ok_at or 0)
        if h.last_ok_at is None:
            broken.append("nenhuma execução bem-sucedida na janela recente")
        elif age > STALE_BROKEN * cadence_s:
            broken.append(f"sem coleta bem-sucedida há {_fmt_age(age)}")
        elif age > STALE_DEGRADED * cadence_s:
            degraded.append(f"última coleta boa há {_fmt_age(age)}, acima do esperado")

    if last["status"] in (jobs.PARTIAL, jobs.FAILED) and not broken:
        degraded.append(f"a última execução foi {last['status']}"
                        + (f" ({last['error_kind']})" if last["error_kind"] else ""))
    if h.failure_rate > FAILURE_RATE_DEGRADED and h.consecutive_failures < CONSECUTIVE_FAILURES_BROKEN:
        degraded.append(f"{int(h.failure_rate * 100)}% das execuções recentes falharam")
    if h.p50_latency_ms is not None and h.p50_latency_ms > LATENCY_DEGRADED_MS:
        degraded.append(f"latência mediana alta: {h.p50_latency_ms} ms")

    if broken:
        h.status, h.reasons = BROKEN, broken + degraded
    elif degraded:
        h.status, h.reasons = DEGRADED, degraded
    return h


def check_source_health(now: int | None = None) -> list[SourceHealth]:
    """Health of every source: registered providers plus any other job that has
    run (ratings refresh, backups). Function meant for the dashboard and the MCP."""
    from . import scheduler                     # cadences live there
    now = now or db.now()
    out: list[SourceHealth] = []
    seen: set[str] = set()
    for status in providers.status():
        name = status["fonte"]
        seen.add(name)
        if not status["configurado"]:
            h = SourceHealth(name, INACTIVE, kind=status["tipo"],
                             reasons=[status["pendencia"]])
            out.append(h)
            continue
        out.append(assess(name, now, scheduler.cadence_for(name), status["tipo"]))
    for name in jobs.sources_with_runs():
        if name not in seen:
            out.append(assess(name, now, scheduler.cadence_for(name), "job"))
    return out


def summarize(items: list[SourceHealth]) -> dict[str, Any]:
    order = {BROKEN: 0, DEGRADED: 1, UNKNOWN: 2, HEALTHY: 3, INACTIVE: 4}
    counts: dict[str, int] = {}
    for h in items:
        counts[h.status] = counts.get(h.status, 0) + 1
    worst = min((h.status for h in items if h.status != INACTIVE),
                key=lambda s: order[s], default=UNKNOWN)
    return {"overall": worst, "overall_label": LABEL[worst], "counts": counts,
            "sources": [h.dict() for h in sorted(items, key=lambda h: order[h.status])]}
