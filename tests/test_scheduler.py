"""Scheduler: ticks, catch-up after sleep, backoff, circuit breaker, alerts."""
import datetime as dt

import pytest

from game_deals import db, health, jobs, providers, scheduler as sch
from game_deals.models import Offer

# A date far from any tracked sale, so the cadence is the base one.
NOW = int(dt.datetime(2026, 8, 3, 12, 0).timestamp())
H = 3600


def offer(price=10000):
    return Offer(source="fake", source_id="x", title="t", store="Loja",
                 price_cents=price, regular_cents=None)


@pytest.fixture()
def world(scratch_db, fake_store, monkeypatch):
    """One fake store with 12 h cadence and one tracked product. Only that
    store runs, so the tests are about the engine and not the other providers."""
    monkeypatch.setitem(sch.CADENCE_S, "fake", 12 * H)
    store = fake_store("fake", {"a": [offer()]})
    db.upsert_product("p1", "P1")
    db.add_alias("p1", "fake", "a")
    db.add_watch("p1", "any_drop", None)
    monkeypatch.setattr(sch, "job_names", lambda: {"fake": "store"})
    monkeypatch.setattr(sch.alerts, "notify", lambda *a, **k: [])
    return store


# ------------------------------------------------------------------- ticks

def test_first_tick_runs_and_second_tick_in_the_same_slot_does_not(world):
    r1 = sch.tick(NOW)
    assert r1.ran == ["fake"]
    r2 = sch.tick(NOW + 60)
    assert r2.ran == [] and r2.not_due == ["fake"]
    assert len(world.calls) == 1


def test_next_tick_runs_again_after_the_cadence(world):
    sch.tick(NOW)
    assert sch.tick(NOW + 12 * H + 60).ran == ["fake"]
    assert len(jobs.runs("fake")) == 2


def test_scheduled_for_is_the_tick_not_the_wall_clock(world):
    sch.tick(NOW + 500)
    row = jobs.last_run("fake")
    c = sch.cadence_for("fake", dt.date.fromtimestamp(NOW))
    assert row["scheduled_for"] == sch.current_tick("fake", NOW + 500, c)
    assert row["scheduled_for"] <= NOW + 500


def test_jitter_is_stable_bounded_and_differs_between_jobs():
    a = sch.jitter_for("nintendo", 12 * H)
    assert a == sch.jitter_for("nintendo", 12 * H)
    assert 0 <= a < 900
    assert len({sch.jitter_for(s, 12 * H) for s in ("nintendo", "playstation", "steam", "itad")}) > 1


# --------------------------------------------------- sleep and catch-up

def test_after_a_long_sleep_one_catch_up_run_and_the_gap_is_reported(world):
    sch.tick(NOW)
    later = NOW + 5 * 12 * H + 100              # the laptop slept through 4 ticks
    rep = sch.tick(later)
    assert rep.ran == ["fake"]                   # ONE run, not four
    assert len(jobs.runs("fake")) == 2
    assert rep.missed_ticks["fake"] == 4


def test_no_missed_ticks_when_running_on_time(world):
    sch.tick(NOW)
    assert sch.tick(NOW + 12 * H + 60).missed_ticks == {}


# ------------------------------------------------- failures and breaker

def test_failed_run_is_retried_only_after_the_backoff(world):
    world.answers["a"] = "page: no product state found (format changed?)"
    sch.tick(NOW)
    assert jobs.last_run("fake")["status"] == jobs.FAILED
    assert sch.tick(NOW + 60).ran == []                        # too soon
    assert sch.tick(NOW + sch.retry_delay(1, 12 * H) + 60).ran == ["fake"]


def test_three_failures_open_the_circuit_and_it_skips(world):
    world.answers["a"] = "answered 503"
    t = NOW
    for _ in range(3):
        last_fail = t
        sch.tick(t)
        t += sch.RETRY_BASE_S * 8
    st = jobs.state("fake")
    assert st["consecutive_failures"] == 3
    assert st["circuit_open_until"] == last_fail + sch.circuit_delay(3)
    rep = sch.tick(last_fail + 60)                  # inside the open window
    assert rep.ran == [] and "disjuntor" in rep.skipped["fake"]


def test_a_good_run_closes_the_circuit(world):
    world.answers["a"] = "answered 503"
    t = NOW
    for _ in range(3):
        sch.tick(t)
        t += sch.RETRY_BASE_S * 8
    world.answers["a"] = [offer()]
    sch.tick(t + sch.CIRCUIT_MAX_S + 1)
    st = jobs.state("fake")
    assert st["consecutive_failures"] == 0 and st["circuit_open_until"] == 0


def test_circuit_delay_grows_and_is_capped():
    assert sch.circuit_delay(3) == 900
    assert sch.circuit_delay(4) == 1800
    assert sch.circuit_delay(30) == sch.CIRCUIT_MAX_S


def test_force_ignores_the_slot_and_the_circuit(world):
    sch.tick(NOW)
    jobs.set_state("fake", circuit_open_until=NOW + 10 * H)
    assert sch.tick(NOW + 60, force=True).ran == ["fake"]


# ---------------------------------------------------------- event boost

def test_cadence_is_halved_before_a_major_sale_but_not_for_estimates():
    assert sch.cadence_for("nintendo", dt.date(2026, 11, 25)) == 6 * H
    assert sch.cadence_for("nintendo", dt.date(2026, 8, 3)) == 12 * H
    assert sch.cadence_for("nintendo", dt.date(2027, 3, 8)) == 12 * H   # estimated only
    assert sch.cadence_for("backup", dt.date(2026, 11, 25)) == 24 * H   # never boosted


# --------------------------------------------------------- health alerts

def test_notifies_once_when_a_source_breaks_and_once_when_it_recovers(world, monkeypatch):
    sent = []
    monkeypatch.setattr(sch.alerts, "notify", lambda t, m="", u="": sent.append(t) or [])
    world.answers["a"] = "page: no product state found (format changed?)"
    t = NOW
    for _ in range(3):
        sch.tick(t)
        t += sch.RETRY_BASE_S * 8
    assert sent.count("Fonte quebrou: fake") == 1
    sch.tick(t)                                   # still broken: no second alert
    assert sent.count("Fonte quebrou: fake") == 1
    world.answers["a"] = [offer()]
    sch.tick(t + sch.CIRCUIT_MAX_S + 1, force=True)
    assert sent.count("Fonte voltou: fake") == 1        # left "broken": that is the news


def test_serve_loops_until_stopped_and_survives_a_bad_tick(world, monkeypatch):
    calls = []

    def flaky_tick(**kw):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("first tick blows up")
        return sch.TickReport(0)

    monkeypatch.setattr(sch, "tick", flaky_tick)
    slept = []
    sch.serve(1, sleep=slept.append, stop=lambda: len(slept) >= 3)
    assert len(calls) == 3                          # kept going after the exception
