"""Gap-aware confidence: expected vs actual runs, judged per source cadence."""
import pytest

from game_deals import db, jobs, rhythm, scheduler as sch, verdict
from game_deals.verdict import evaluate, highlight

NOW = 1_800_000_000
D, H = 86400, 3600
SRC = "nintendo"          # cadence 12 h


@pytest.fixture(autouse=True)
def _db(scratch_db):
    db.upsert_product("p", "Jogo")


def runs(source, cadence, start, end, skip=(), status=jobs.OK):
    """One run per slot between start and end, minus the `skip` windows."""
    t = start
    while t < end:
        if not any(a <= t < b for a, b in skip):
            jobs.record(source, status, started_at=t, finished_at=t + 30,
                        items=3, attempted=3, scheduled_for=t)
        t += cadence


def history(days=100, store="Nintendo eShop", source=SRC):
    """A history long enough for 'alta' confidence: a point every 2 days."""
    for d in range(days, 0, -2):
        db.record("p", source, store, 30000, None, "BRL", True, "", ts=NOW - d * D)


def new_low(now=NOW, price=25000):
    return evaluate("p", "Nintendo eShop", price, now=now)


# ---------------------------------------------------------------- unchanged

def test_no_job_log_means_no_judgement_and_no_penalty():
    """History that predates the scheduler, or was seeded elsewhere."""
    history()
    v = new_low()
    assert v.gap is None and v.confidence == v.confidence_before_gaps == "alta"


def test_perfect_rhythm_keeps_confidence_and_says_so():
    history()
    runs(SRC, 12 * H, NOW - 100 * D, NOW)
    v = new_low()
    assert v.confidence == "alta"
    assert v.gap["severity"] == "none" and v.gap["ratio"] == 1.0
    assert "em dia" in v.gap["text"]


def test_only_the_period_covered_by_the_job_log_is_judged():
    history(days=300)
    runs(SRC, 12 * H, NOW - 20 * D, NOW)               # log starts 20 days ago
    r = rhythm.assess_rhythm(SRC, window_days=300, now=NOW)
    assert r.covered_days == 20 and r.expected == 40 and r.severity == "none"


def test_too_short_a_log_cannot_be_judged():
    runs(SRC, 12 * H, NOW - 1 * D, NOW)
    assert rhythm.assess_rhythm(SRC, 100, NOW) is None       # < 3 expected runs


# ----------------------------------------------------------------- downgrade

def test_a_minor_gap_costs_one_level_and_explains_itself():
    history()
    runs(SRC, 12 * H, NOW - 100 * D, NOW, skip=[(NOW - 40 * D, NOW - 30 * D)])
    v = new_low()
    assert v.gap["severity"] == "minor"
    assert (v.confidence_before_gaps, v.confidence) == ("alta", "media")
    assert any("lacuna de até 10 dias" in r for r in v.confidence_reasons)
    assert "lacuna" in v.caveat


def test_a_major_gap_costs_two_levels_and_kills_the_badge():
    history()
    runs(SRC, 12 * H, NOW - 100 * D, NOW, skip=[(NOW - 60 * D, NOW - 44 * D)])
    v = new_low()
    assert v.gap["severity"] == "major"
    assert (v.confidence_before_gaps, v.confidence) == ("alta", "baixa")
    assert highlight(v) is None                # a "new low" with a hole in it


def test_ratio_below_sixty_percent_is_major_even_without_one_long_gap():
    history()
    # runs only on every other 12 h slot: 50% coverage, gaps of a day
    for i in range(0, 200, 2):
        t = NOW - 100 * D + i * 12 * H
        jobs.record(SRC, jobs.OK, started_at=t, finished_at=t + 30, items=1,
                    attempted=1, scheduled_for=t)
    r = rhythm.assess_rhythm(SRC, 100, NOW)
    assert r.ratio < 0.6 and r.severity == "major"


def test_confidence_never_goes_below_baixa_and_baixa_is_left_alone():
    assert verdict.apply_gaps("baixa", _report("major")) == ("baixa", "")
    assert verdict.apply_gaps("media", _report("major"))[0] == "baixa"
    assert verdict.apply_gaps("alta", None) == ("alta", "")


def _report(sev):
    return rhythm.GapReport("s", 43200, 30, 30, 60, 20, 0.33, 10 * D, sev)


# --------------------------------------------------- own cadence, not daily

def test_a_weekly_job_that_ran_every_week_is_not_penalized(monkeypatch):
    monkeypatch.setitem(sch.CADENCE_S, "weekly_src", 7 * D)
    runs("weekly_src", 7 * D, NOW - 84 * D, NOW)               # 12 weekly runs
    r = rhythm.assess_rhythm("weekly_src", 84, NOW)
    assert r.severity == "none" and r.expected == 12 and r.actual == 12
    # Judged against a daily assumption the same runs would look terrible:
    wrong = rhythm.assess_rhythm("weekly_src", 84, NOW, cadence_s=D)
    assert wrong.severity == "major"


def test_seeded_sparse_history_from_another_source_is_not_a_daily_gap():
    """ITAD backfill: monthly points, no runs at all for that source."""
    for d in range(300, 0, -30):
        db.record("p", "itad", "Steam", 30000, None, "BRL", True, "", ts=NOW - d * D)
    v = evaluate("p", "Steam", 25000, now=NOW)
    assert v.gap is None


# ------------------------------------------------------ what counts as a run

def test_failed_runs_do_not_count_as_coverage():
    runs(SRC, 12 * H, NOW - 30 * D, NOW, status=jobs.FAILED)
    r = rhythm.assess_rhythm(SRC, 30, NOW)
    assert r.actual == 0 and r.severity == "major"


def test_partial_runs_do_count():
    runs(SRC, 12 * H, NOW - 30 * D, NOW, status=jobs.PARTIAL)
    assert rhythm.assess_rhythm(SRC, 30, NOW).severity == "none"


def test_other_sources_runs_do_not_fill_the_gap():
    runs(SRC, 12 * H, NOW - 30 * D, NOW, skip=[(NOW - 20 * D, NOW - 10 * D)])
    runs("steam", 6 * H, NOW - 30 * D, NOW)
    assert rhythm.assess_rhythm(SRC, 30, NOW).severity != "none"


def test_a_catch_up_run_after_a_long_sleep_does_not_hide_the_gap():
    runs(SRC, 12 * H, NOW - 30 * D, NOW - 12 * D)
    jobs.record(SRC, jobs.OK, started_at=NOW - 6 * D, finished_at=NOW - 6 * D + 30,
                items=1, attempted=1)                       # one catch-up run
    runs(SRC, 12 * H, NOW - 5 * D, NOW)
    r = rhythm.assess_rhythm(SRC, 30, NOW)
    assert r.longest_gap_s >= 6 * D and r.severity != "none"


def test_two_runs_in_the_same_slot_count_once():
    for i in range(6):
        jobs.record(SRC, jobs.OK, started_at=NOW - 3 * D + i * 60, finished_at=NOW - 3 * D + i * 60 + 5,
                    items=1, attempted=1)
    runs(SRC, 12 * H, NOW - 30 * D, NOW - 3 * D - 12 * H)
    runs(SRC, 12 * H, NOW - 2 * D, NOW)
    r = rhythm.assess_rhythm(SRC, 30, NOW)
    assert r.actual <= r.expected


def test_verdict_payload_carries_the_structured_gap():
    history()
    runs(SRC, 12 * H, NOW - 100 * D, NOW, skip=[(NOW - 40 * D, NOW - 30 * D)])
    d = new_low().dict()
    assert d["gap"]["longest_gap_days"] >= 10 and d["gap"]["cadence_s"] == 12 * H
    assert d["confidence_before_gaps"] == "alta" and d["confidence"] == "media"
    assert isinstance(d["confidence_reasons"], list)
