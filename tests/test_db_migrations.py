"""Versioned migrations and the deal_signals compound key."""
import sqlite3

import pytest

from game_deals import config, db

OLD_SCHEMA_SIGNALS = """
CREATE TABLE deal_signals (
  source TEXT NOT NULL, source_id TEXT NOT NULL, product_id TEXT,
  title TEXT NOT NULL, store TEXT NOT NULL DEFAULT '',
  price_cents INTEGER NOT NULL, old_price_cents INTEGER,
  discount_pct REAL NOT NULL DEFAULT 0, url TEXT NOT NULL DEFAULT '',
  image TEXT NOT NULL DEFAULT '', published_ts INTEGER NOT NULL DEFAULT 0,
  seen_ts INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (source, source_id));
"""


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(config, "DB_PATH", str(path))
    monkeypatch.setattr(db, "_conn", None)
    yield path
    if db._conn is not None:
        db._conn.close()
    monkeypatch.setattr(db, "_conn", None)


def _pk(path, table):
    c = sqlite3.connect(path)
    cols = [r[1] for r in sorted(c.execute(f"PRAGMA table_info({table})"),
                                 key=lambda r: r[5]) if r[5]]
    c.close()
    return cols


def test_same_offer_can_belong_to_two_products(fresh_db):
    """Regression: GTA VI Standard and Ultimate match the same retail offers.
    With key (source, source_id) the last product overwrote the first."""
    db.upsert_product("gta6-std", "Grand Theft Auto VI")
    db.upsert_product("gta6-ult", "Grand Theft Auto VI: Ultimate Edition")
    args = ("promobit", "123", None, "Jogo GTA VI PS5", "Netshoes", 34417,
            None, 0.0, "u", "", 0)
    assert db.upsert_signal(args[0], args[1], "gta6-std", *args[3:])
    assert db.upsert_signal(args[0], args[1], "gta6-ult", *args[3:])
    assert len(db.signals_for("gta6-std")) == 1
    assert len(db.signals_for("gta6-ult")) == 1


def test_upsert_is_idempotent_per_product(fresh_db):
    db.upsert_product("p", "P")
    a = ("promobit", "1", "p", "t", "L", 100, None, 0.0, "", "", 0)
    assert db.upsert_signal(*a) is True       # new
    assert db.upsert_signal(*a) is False      # already known


def test_fresh_database_is_at_latest_version(fresh_db):
    db.conn()
    v = db.conn().execute("SELECT MAX(version) v FROM schema_version").fetchone()["v"]
    assert v == db.LATEST_VERSION
    assert _pk(fresh_db, "deal_signals") == ["source", "source_id", "product_id"]


def test_old_database_is_upgraded_and_keeps_rows(fresh_db):
    raw = sqlite3.connect(fresh_db)
    raw.executescript(OLD_SCHEMA_SIGNALS)
    raw.execute("INSERT INTO deal_signals VALUES "
                "('promobit','9','p','t','L',100,NULL,0,'','',0,1,1)")
    raw.execute("INSERT INTO deal_signals VALUES "
                "('promobit','10',NULL,'orphan','L',200,NULL,0,'','',0,1,1)")
    raw.commit()
    raw.close()

    db.conn()                                   # triggers the migration
    assert _pk(fresh_db, "deal_signals") == ["source", "source_id", "product_id"]
    rows = db.conn().execute(
        "SELECT source_id, product_id, price_cents FROM deal_signals "
        "ORDER BY source_id").fetchall()
    assert [(r["source_id"], r["product_id"], r["price_cents"]) for r in rows] == [
        ("10", "", 200), ("9", "p", 100)]       # NULL product becomes ''


def test_migrations_are_idempotent(fresh_db):
    db.conn()
    db._conn.close()
    db._conn = None
    db.conn()
    versions = [r["version"] for r in
                db.conn().execute("SELECT version FROM schema_version")]
    assert len(versions) == len(set(versions))
