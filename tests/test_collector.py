"""Collector: one logged run per source, honest failure accounting."""
import httpx
import pytest

from game_deals import db, http, jobs
from game_deals import collector
from game_deals.models import Offer


def offer(price=10000, store="Loja"):
    return Offer(source="fake", source_id="x", title="t", store=store,
                 price_cents=price, regular_cents=None)


def track(product_id, source, source_id, rule="any_drop"):
    db.upsert_product(product_id, product_id.upper())
    db.add_alias(product_id, source, source_id)
    db.add_watch(product_id, rule, None)


def test_one_run_per_source_is_logged_with_counts(scratch_db, fake_store):
    fake_store("fake", {"a": [offer()], "b": [offer(), offer(store="Outra")]})
    track("p1", "fake", "a")
    track("p2", "fake", "b")
    runs = collector.collect()
    r = runs["fake"]
    assert (r.attempted, r.items, r.failed, r.status) == (2, 3, 0, jobs.OK)
    row = jobs.last_run("fake")
    assert row["status"] == jobs.OK and row["items"] == 3 and row["attempted"] == 2
    assert row["finished_at"] is not None


def test_product_not_sold_is_not_a_source_failure(scratch_db, fake_store):
    """A game missing from the store is a fact about the game. Counting it as a
    failure would make a healthy source look broken."""
    fake_store("fake", {"a": [offer()], "b": "NSUID 1 does not price in BR"})
    track("p1", "fake", "a")
    track("p2", "fake", "b")
    r = collector.collect()["fake"]
    assert r.failed == 0 and r.status == jobs.OK and r.items == 1


def test_real_failures_make_the_run_partial_or_failed(scratch_db, fake_store):
    fake_store("fake", {"a": [offer()], "b": "page: no product state found (format changed?)"})
    track("p1", "fake", "a")
    track("p2", "fake", "b")
    r = collector.collect()["fake"]
    assert (r.failed, r.status, r.error_kind) == (1, jobs.PARTIAL, "drift")

    fake_store("fake", {"a": httpx.ReadTimeout("slow"), "b": "answered 503"})
    r = collector.collect()["fake"]
    assert r.status == jobs.FAILED and r.failed == 2


def test_robots_refusal_is_classified_as_robots(scratch_db, fake_store):
    fake_store("fake", {"a": http.RobotsBlocked("https://x/y")})
    track("p1", "fake", "a")
    r = collector.collect()["fake"]
    assert r.error_kind == "robots" and r.status == jobs.FAILED


def test_one_broken_alias_does_not_stop_the_others(scratch_db, fake_store):
    fake_store("fake", {"a": RuntimeError("boom"), "b": [offer(555)]})
    track("p1", "fake", "a")
    track("p2", "fake", "b")
    collector.collect()
    assert db.last_price("p2", "Loja")["price_cents"] == 555


def test_unwatched_products_are_not_collected(scratch_db, fake_store):
    p = fake_store("fake", {"a": [offer()]})
    db.upsert_product("p1", "P1")
    db.add_alias("p1", "fake", "a")          # tracked but no watch
    assert collector.collect() == {} and p.calls == []


def test_prices_are_recorded_and_alerts_use_the_previous_reading(scratch_db, fake_store):
    fake_store("fake", {"a": [offer(9000)]})
    track("p1", "fake", "a")
    db.record("p1", "fake", "Loja", 12000, None, "BRL", True, "", ts=db.now() - 86400)
    r = collector.collect()["fake"]
    assert len(r.alerts) == 1 and "caiu" in r.alerts[0]["motivo"]   # any_drop
    assert db.last_price("p1", "Loja")["price_cents"] == 9000


def test_error_classification_table():
    c = jobs.classify_error
    assert c("robots.txt forbids automated access to x") == "robots"
    assert c("page: no product state found (format changed?)") == "drift"
    assert c("NSUID 1 does not price in BR; European NSUIDs are not valid") == "not_found"
    assert c("no 'deluxe' edition on concept 1; available: standard") == "not_found"
    assert c("concept 1 has several editions (standard, ultimate)") == "ambiguous"
    assert c("https://x answered 503") == "http"
    assert c("") == ""
    assert c(exc=httpx.ReadTimeout("t")) == "timeout"
    assert c(exc=httpx.ConnectError("c")) == "http"
    assert c(exc=KeyError("k")) == "drift"


def test_stale_running_rows_are_abandoned(scratch_db):
    rid = jobs.start("fake", now=db.now() - 7200)
    assert jobs.abandon_stale(3600) == 1
    row = db.conn().execute("SELECT status,error_kind FROM job_runs WHERE id=?",
                            (rid,)).fetchone()
    assert (row["status"], row["error_kind"]) == (jobs.FAILED, "other")
