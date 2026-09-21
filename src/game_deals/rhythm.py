"""Did we actually look, as often as we meant to?

A price history is only as good as the times somebody was watching. "Lowest in
7 months" means little if the collector was off for five of them. This module
compares the runs a source SHOULD have made in a window with the runs it made,
using job_runs, and measures the longest stretch with no good run.

Two rules keep it honest:

- Expected runs come from the source's OWN scheduled cadence. A weekly job that
  ran every week is perfect, not 6/7 missing against a daily assumption.
- Only the period covered by the job log is judged. History that predates the
  scheduler, or was seeded from elsewhere (ITAD backfill), has no runs to
  compare with, so it is neither rewarded nor penalized.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from . import db, jobs

DAY = 86400
MIN_EXPECTED = 3                 # fewer expected runs than this cannot be judged
MAJOR_RATIO, MINOR_RATIO = 0.60, 0.85
MAJOR_GAP_S = 14 * DAY
MINOR_GAP_FLOOR_S = 2 * DAY
MINOR_GAP_CADENCES = 5


@dataclass
class GapReport:
    source: str
    cadence_s: int
    window_days: int             # what was asked
    covered_days: int            # what the job log actually covers
    expected: int
    actual: int
    ratio: float
    longest_gap_s: int
    severity: str                # none | minor | major

    def dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["longest_gap_days"] = round(self.longest_gap_s / DAY, 1)
        d["text"] = self.text()
        return d

    def text(self) -> str:
        if self.severity == "none":
            return (f"coleta em dia: {self.actual} de {self.expected} execuções "
                    f"previstas ({_cadence(self.cadence_s)}) nos últimos {self.covered_days} dias")
        gap = self.longest_gap_s / DAY
        gap_txt = f"{gap:.0f} dias" if gap >= 1 else f"{self.longest_gap_s // 3600} h"
        return (f"a coleta rodou {self.actual} de {self.expected} vezes previstas "
                f"({_cadence(self.cadence_s)}) nos últimos {self.covered_days} dias, "
                f"com uma lacuna de até {gap_txt}")


def _cadence(seconds: int) -> str:
    if seconds % DAY == 0:
        d = seconds // DAY
        return "1 vez por dia" if d == 1 else f"a cada {d} dias"
    return f"a cada {seconds // 3600} h"


def base_cadence(source: str) -> int:
    from . import scheduler                     # lazy: scheduler imports the collector
    return scheduler.CADENCE_S.get(source, scheduler.DEFAULT_CADENCE_S)


def assess_rhythm(source: str, window_days: int, now: int | None = None,
                  cadence_s: int | None = None) -> GapReport | None:
    """None when there is nothing to judge: no job log for this source, or too
    short a covered period to expect at least MIN_EXPECTED runs."""
    now = now or db.now()
    first = jobs.first_run(source)
    if first is None:
        return None
    cadence = cadence_s or base_cadence(source)
    covered_from = max(now - window_days * DAY, first["started_at"])
    span = now - covered_from
    expected = span // cadence
    if expected < MIN_EXPECTED:
        return None

    rows = jobs.runs(source, since=covered_from, until=now,
                     statuses=(jobs.OK, jobs.PARTIAL))
    slots = {r["started_at"] // cadence for r in rows}    # distinct time slots
    actual = min(len(slots), expected)
    times = sorted(r["started_at"] for r in rows)
    edges = [covered_from] + times + [now]
    longest = max(b - a for a, b in zip(edges, edges[1:]))
    ratio = round(actual / expected, 3)

    minor_gap = max(MINOR_GAP_CADENCES * cadence, MINOR_GAP_FLOOR_S)
    if ratio < MAJOR_RATIO or longest >= MAJOR_GAP_S:
        severity = "major"
    elif ratio < MINOR_RATIO or longest > minor_gap:
        severity = "minor"
    else:
        severity = "none"
    return GapReport(source, cadence, window_days, span // DAY, expected, actual,
                     ratio, longest, severity)
