"""Source health, judged only from job_runs."""
import pytest

from game_deals import db, health, jobs, scheduler as sch

NOW = 1_800_000_000
H, D = 3600, 86400


@pytest.fixture(autouse=True)
def _db(scratch_db, monkeypatch):
    monkeypatch.setitem(sch.CADENCE_S, "src", 12 * H)


def run(minutes_ago, status=jobs.OK, items=5, attempted=5, failed=0,
        kind="", latency=300):
    t = NOW - minutes_ago * 60
    jobs.record("src", status, started_at=t, finished_at=t + 20, items=items,
                attempted=attempted, failed=failed, error_kind=kind,
                latency_ms=latency, scheduled_for=t)


def status():
    return health.assess("src", NOW, 12 * H)


def test_no_runs_is_unknown_not_healthy():
    h = status()
    assert h.status == health.UNKNOWN and "nenhuma execução" in h.reasons[0]


def test_regular_good_runs_are_healthy():
    for i in range(5):
        run(60 + i * 12 * 60)
    assert status().status == health.HEALTHY


def test_a_partial_last_run_is_degraded():
    run(60 * 13)
    run(60, jobs.PARTIAL, failed=2, kind="http")
    h = status()
    assert h.status == health.DEGRADED and "partial" in h.reasons[0]


def test_three_failures_in_a_row_is_broken():
    run(5 * 12 * 60)
    for i in (3, 2, 1):
        run(i * 12 * 60 - 10, jobs.FAILED, items=0, failed=5, kind="http")
    h = status()
    assert h.status == health.BROKEN and h.consecutive_failures == 3


def test_consecutive_zero_items_is_broken_only_if_it_used_to_return_items():
    """Distinguishes 'the source went quiet' from 'nothing tracked is sold'."""
    run(5 * 12 * 60, items=8)
    for i in (3, 2, 1):
        run(i * 12 * 60 - 10, items=0)
    h = status()
    assert h.status == health.BROKEN and "passou a não trazer itens" in " ".join(h.reasons)


def test_never_returning_items_is_only_degraded():
    for i in (3, 2, 1):
        run(i * 12 * 60 - 10, items=0)
    h = status()
    assert h.status == health.DEGRADED and "nunca trouxe itens" in h.reasons[0]


def test_two_drift_errors_in_a_row_is_broken_one_is_degraded():
    run(3 * 12 * 60)
    run(2 * 12 * 60, jobs.PARTIAL, failed=1, kind="drift")
    assert status().status == health.DEGRADED
    run(12 * 60, jobs.PARTIAL, failed=1, kind="drift")
    h = status()
    assert h.status == health.BROKEN and "formato" in " ".join(h.reasons)


def test_robots_refusal_is_broken():
    run(12 * 60)
    run(30, jobs.FAILED, items=0, failed=5, kind="robots")
    h = status()
    assert h.status == health.BROKEN and "robots.txt" in " ".join(h.reasons)


def test_staleness_degrades_then_breaks_by_cadence():
    run(60)                                            # one good run 1 h ago, then silence
    assert health.assess("src", NOW, 12 * H).status == health.HEALTHY
    assert health.assess("src", NOW + 30 * H, 12 * H).status == health.DEGRADED   # > 2 cadences
    assert health.assess("src", NOW + 50 * H, 12 * H).status == health.BROKEN     # > 4 cadences


def test_high_latency_is_degraded():
    for i in range(5):
        run(60 + i * 12 * 60, latency=12000)
    h = status()
    assert h.status == health.DEGRADED and "latência" in h.reasons[0]


def test_open_circuit_is_broken():
    run(60)
    jobs.set_state("src", circuit_open_until=NOW + 2 * H)
    h = status()
    assert h.status == health.BROKEN and "disjuntor" in h.reasons[0]


def test_summary_orders_worst_first_and_counts(monkeypatch):
    monkeypatch.setattr(health, "check_source_health", lambda now=None: [
        health.SourceHealth("a", health.HEALTHY), health.SourceHealth("b", health.BROKEN),
        health.SourceHealth("c", health.INACTIVE)])
    s = health.summarize(health.check_source_health())
    assert s["overall"] == health.BROKEN
    assert [x["source"] for x in s["sources"]] == ["b", "a", "c"]
    assert s["counts"] == {"healthy": 1, "broken": 1, "inactive": 1}
    assert s["sources"][0]["label"] == "quebrada"


def test_unconfigured_providers_are_inactive_and_say_why(monkeypatch):
    res = health.check_source_health(NOW)
    inactive = [h for h in res if h.status == health.INACTIVE]
    assert {h.source for h in inactive} >= {"itad", "mercadolivre", "amazon", "shopee"}
    assert all(h.reasons and h.reasons[0] for h in inactive)
