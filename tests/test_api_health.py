from fastapi.testclient import TestClient

from game_deals import jobs, web

NOW = 1_800_000_000


def test_health_endpoint_returns_a_worst_first_summary(scratch_db):
    for i in (3, 2, 1):
        jobs.record("nintendo", jobs.FAILED, started_at=NOW - i * 3600, items=0,
                    attempted=2, failed=2, error_kind="http")
    r = TestClient(web.app).get("/api/health")
    body = r.json()
    assert r.status_code == 200
    assert body["overall"] in ("broken", "degraded", "unknown", "healthy")
    names = {s["source"]: s for s in body["sources"]}
    assert {"nintendo", "playstation", "steam"} <= set(names)
    assert names["itad"]["status"] == "inactive" and names["itad"]["reasons"]
    assert all("label" in s for s in body["sources"])


def test_verdict_confidence_reasons_are_exposed_in_the_mcp_price_payload():
    from game_deals.verdict import Verdict
    v = Verdict("x", False, None, None, None, None, 1, 1, "media",
                confidence_before_gaps="alta", confidence_reasons=["lacuna"])
    d = v.dict()
    assert d["confidence_reasons"] == ["lacuna"] and d["confidence_before_gaps"] == "alta"
