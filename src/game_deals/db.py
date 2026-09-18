"""SQLite: catalogo, aliases, serie temporal de precos, watchlist e alertas.

A serie temporal e o coracao do projeto. Sem ela nao existe "melhor preco em X".
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Iterable

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
  id          TEXT PRIMARY KEY,
  title       TEXT NOT NULL,
  category    TEXT NOT NULL DEFAULT 'game',   -- game | hardware
  platform    TEXT NOT NULL DEFAULT '',       -- pc | switch | ps5 | xbox | ''
  image_url   TEXT NOT NULL DEFAULT '',
  rawg_id     INTEGER,
  metacritic  INTEGER,
  user_rating REAL,
  ratings_count INTEGER NOT NULL DEFAULT 0,
  popularity  INTEGER NOT NULL DEFAULT 0,   -- proxy, nao vendas reais
  relevance   REAL,
  released    TEXT NOT NULL DEFAULT '',
  compat      TEXT NOT NULL DEFAULT '',   -- como roda no Switch 2
  created_at  INTEGER NOT NULL
);

-- Identidade de produto entre fontes. Popule a mao: fuzzy match gera alerta errado.
CREATE TABLE IF NOT EXISTS aliases (
  product_id  TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  source      TEXT NOT NULL,
  source_id   TEXT NOT NULL,
  label       TEXT NOT NULL DEFAULT '',
  url         TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_aliases_product ON aliases(product_id);

CREATE TABLE IF NOT EXISTS price_points (
  product_id   TEXT NOT NULL,
  source       TEXT NOT NULL,
  store        TEXT NOT NULL DEFAULT '',
  price_cents  INTEGER NOT NULL,
  regular_cents INTEGER,
  currency     TEXT NOT NULL DEFAULT 'BRL',
  in_stock     INTEGER NOT NULL DEFAULT 1,
  url          TEXT NOT NULL DEFAULT '',
  ts           INTEGER NOT NULL,
  PRIMARY KEY (product_id, source, store, ts)
);
CREATE INDEX IF NOT EXISTS idx_pp_lookup ON price_points(product_id, price_cents, ts);

CREATE TABLE IF NOT EXISTS watches (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  product_id  TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  rule_kind   TEXT NOT NULL,      -- price_below|discount_above|new_low|any_drop|back_in_stock
  rule_value  REAL,
  note        TEXT NOT NULL DEFAULT '',
  active      INTEGER NOT NULL DEFAULT 1,
  created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  watch_id    INTEGER,
  product_id  TEXT NOT NULL,
  source      TEXT NOT NULL,
  store       TEXT NOT NULL DEFAULT '',
  price_cents INTEGER NOT NULL,
  headline    TEXT NOT NULL,
  url         TEXT NOT NULL DEFAULT '',
  ts          INTEGER NOT NULL,
  acknowledged INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alerts_open ON alerts(acknowledged, ts);

-- Ofertas postadas por pessoas (Promobit e afins). Tabela SEPARADA de propósito:
-- um post é um evento pontual, não uma leitura periódica da mesma loja. Misturar
-- com price_points envenenaria o veredito "menor preço em X tempo".
CREATE TABLE IF NOT EXISTS deal_signals (
  source       TEXT NOT NULL,
  source_id    TEXT NOT NULL,
  product_id   TEXT,                       -- NULL = sinal ainda não casado
  title        TEXT NOT NULL,
  store        TEXT NOT NULL DEFAULT '',
  price_cents  INTEGER NOT NULL,
  old_price_cents INTEGER,
  discount_pct REAL NOT NULL DEFAULT 0,
  url          TEXT NOT NULL DEFAULT '',
  image        TEXT NOT NULL DEFAULT '',
  published_ts INTEGER NOT NULL DEFAULT 0,
  seen_ts      INTEGER NOT NULL,
  active       INTEGER NOT NULL DEFAULT 1,   -- 0 = oferta encerrada (histórica)
  PRIMARY KEY (source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_signals_prod ON deal_signals(product_id, price_cents);
"""

_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.executescript(SCHEMA)
        _migrate(_conn)
        _conn.commit()
    return _conn


def _migrate(c: sqlite3.Connection) -> None:
    """Colunas adicionadas depois do primeiro release. CREATE TABLE IF NOT EXISTS
    nao altera tabela existente, entao a adicao vai aqui."""
    cols = {r["name"] for r in c.execute("PRAGMA table_info(products)").fetchall()}
    novas = [
        ("image_url", "TEXT NOT NULL DEFAULT ''"),
        ("rawg_id", "INTEGER"),
        ("metacritic", "INTEGER"),
        ("user_rating", "REAL"),
        ("ratings_count", "INTEGER NOT NULL DEFAULT 0"),
        ("popularity", "INTEGER NOT NULL DEFAULT 0"),
        ("relevance", "REAL"),
        ("released", "TEXT NOT NULL DEFAULT ''"),
        ("compat", "TEXT NOT NULL DEFAULT ''"),
    ]
    for nome, tipo in novas:
        if nome not in cols:
            c.execute(f"ALTER TABLE products ADD COLUMN {nome} {tipo}")

    sig = {r["name"] for r in c.execute("PRAGMA table_info(deal_signals)").fetchall()}
    if sig and "active" not in sig:
        c.execute("ALTER TABLE deal_signals ADD COLUMN active INTEGER NOT NULL DEFAULT 1")


def now() -> int:
    return int(time.time())


# ---------- catalogo ----------

def upsert_product(product_id: str, title: str, category: str = "game",
                   platform: str = "", image_url: str = "",
                   compat: str = "") -> None:
    conn().execute(
        "INSERT INTO products(id,title,category,platform,image_url,compat,created_at)"
        " VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET title=excluded.title, "
        "category=excluded.category, platform=excluded.platform, "
        # nao apaga imagem existente com string vazia
        "image_url=CASE WHEN excluded.image_url<>'' THEN excluded.image_url "
        "ELSE products.image_url END, "
        "compat=CASE WHEN excluded.compat<>'' THEN excluded.compat "
        "ELSE products.compat END",
        (product_id, title, category, platform, image_url, compat, now()))
    conn().commit()


def set_ratings(product_id: str, rawg_id: int | None, metacritic: int | None,
                user_rating: float | None, ratings_count: int, popularity: int,
                relevance: float | None, released: str = "") -> None:
    conn().execute(
        "UPDATE products SET rawg_id=?, metacritic=?, user_rating=?, "
        "ratings_count=?, popularity=?, relevance=?, released=? WHERE id=?",
        (rawg_id, metacritic, user_rating, ratings_count, popularity,
         relevance, released, product_id))
    conn().commit()


def set_image(product_id: str, image_url: str) -> None:
    if not image_url:
        return
    conn().execute(
        "UPDATE products SET image_url=? WHERE id=? AND image_url=''",
        (image_url, product_id))
    conn().commit()


def add_alias(product_id: str, source: str, source_id: str,
              label: str = "", url: str = "") -> None:
    conn().execute(
        "INSERT INTO aliases(product_id,source,source_id,label,url) VALUES (?,?,?,?,?) "
        "ON CONFLICT(source,source_id) DO UPDATE SET product_id=excluded.product_id, "
        "label=excluded.label, url=excluded.url",
        (product_id, source, source_id, label, url))
    conn().commit()


def aliases_for(product_id: str) -> list[sqlite3.Row]:
    return conn().execute(
        "SELECT * FROM aliases WHERE product_id=?", (product_id,)).fetchall()


def get_product(product_id: str) -> sqlite3.Row | None:
    return conn().execute(
        "SELECT * FROM products WHERE id=?", (product_id,)).fetchone()


def list_products() -> list[sqlite3.Row]:
    return conn().execute("SELECT * FROM products ORDER BY title").fetchall()


def resolve(query: str) -> str | None:
    """Aceita id exato, ou casa por prefixo/substring de titulo."""
    if get_product(query):
        return query
    row = conn().execute(
        "SELECT id FROM products WHERE lower(title) LIKE lower(?) ORDER BY length(title) LIMIT 1",
        (f"%{query}%",)).fetchone()
    return row["id"] if row else None


# ---------- precos ----------

def record(product_id: str, source: str, store: str, price_cents: int,
           regular_cents: int | None, currency: str, in_stock: bool,
           url: str, ts: int | None = None) -> None:
    conn().execute(
        "INSERT OR REPLACE INTO price_points"
        "(product_id,source,store,price_cents,regular_cents,currency,in_stock,url,ts)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (product_id, source, store, price_cents, regular_cents, currency,
         1 if in_stock else 0, url, ts or now()))
    conn().commit()


def record_many(rows: Iterable[tuple]) -> int:
    rows = list(rows)
    conn().executemany(
        "INSERT OR REPLACE INTO price_points"
        "(product_id,source,store,price_cents,regular_cents,currency,in_stock,url,ts)"
        " VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn().commit()
    return len(rows)


def history(product_id: str, store: str | None = None,
            since_ts: int | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM price_points WHERE product_id=?"
    args: list[Any] = [product_id]
    if store:
        sql += " AND store=?"
        args.append(store)
    if since_ts:
        sql += " AND ts>=?"
        args.append(since_ts)
    sql += " ORDER BY ts"
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def last_price(product_id: str, store: str) -> sqlite3.Row | None:
    return conn().execute(
        "SELECT * FROM price_points WHERE product_id=? AND store=? "
        "ORDER BY ts DESC LIMIT 1", (product_id, store)).fetchone()


# ---------- sinais da comunidade ----------

def upsert_signal(source: str, source_id: str, product_id: str | None,
                  title: str, store: str, price_cents: int,
                  old_price_cents: int | None, discount_pct: float,
                  url: str, image: str, published_ts: int,
                  active: bool = True) -> bool:
    """Grava o sinal. Devolve True se for NOVO (para decidir se alerta)."""
    novo = conn().execute(
        "SELECT 1 FROM deal_signals WHERE source=? AND source_id=?",
        (source, source_id)).fetchone() is None
    conn().execute(
        "INSERT INTO deal_signals(source,source_id,product_id,title,store,"
        "price_cents,old_price_cents,discount_pct,url,image,published_ts,seen_ts,"
        "active) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(source,source_id) DO UPDATE SET price_cents=excluded.price_cents,"
        " discount_pct=excluded.discount_pct, product_id=excluded.product_id,"
        " active=excluded.active",
        (source, source_id, product_id, title, store, price_cents,
         old_price_cents, discount_pct, url, image, published_ts, now(),
         1 if active else 0))
    conn().commit()
    return novo


def signals_for(product_id: str, limit: int = 6,
                apenas_ativas: bool = True) -> list[sqlite3.Row]:
    sql = "SELECT * FROM deal_signals WHERE product_id=?"
    if apenas_ativas:
        sql += " AND active=1"
    return conn().execute(sql + " ORDER BY price_cents LIMIT ?",
                          (product_id, limit)).fetchall()


def cheapest_signal(product_id: str) -> sqlite3.Row | None:
    return conn().execute(
        "SELECT * FROM deal_signals WHERE product_id=? ORDER BY price_cents LIMIT 1",
        (product_id,)).fetchone()


# ---------- watchlist ----------

def add_watch(product_id: str, rule_kind: str, rule_value: float | None,
              note: str = "") -> int:
    cur = conn().execute(
        "INSERT INTO watches(product_id,rule_kind,rule_value,note,active,created_at)"
        " VALUES (?,?,?,?,1,?)", (product_id, rule_kind, rule_value, note, now()))
    conn().commit()
    return int(cur.lastrowid)


def active_watches() -> list[sqlite3.Row]:
    return conn().execute(
        "SELECT w.*, p.title, p.category, p.platform FROM watches w "
        "JOIN products p ON p.id=w.product_id WHERE w.active=1 ORDER BY w.id").fetchall()


def set_watch_active(watch_id: int, active: bool) -> None:
    conn().execute("UPDATE watches SET active=? WHERE id=?",
                   (1 if active else 0, watch_id))
    conn().commit()


# ---------- alertas ----------

def add_alert(watch_id: int | None, product_id: str, source: str, store: str,
              price_cents: int, headline: str, url: str) -> int:
    cur = conn().execute(
        "INSERT INTO alerts(watch_id,product_id,source,store,price_cents,headline,url,ts)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (watch_id, product_id, source, store, price_cents, headline, url, now()))
    conn().commit()
    return int(cur.lastrowid)


def open_alerts(limit: int = 50) -> list[sqlite3.Row]:
    return conn().execute(
        "SELECT a.*, p.title FROM alerts a JOIN products p ON p.id=a.product_id "
        "WHERE a.acknowledged=0 ORDER BY a.ts DESC LIMIT ?", (limit,)).fetchall()


def ack_alerts(ids: list[int] | None = None) -> int:
    if ids:
        q = ",".join("?" * len(ids))
        cur = conn().execute(
            f"UPDATE alerts SET acknowledged=1 WHERE id IN ({q})", ids)
    else:
        cur = conn().execute("UPDATE alerts SET acknowledged=1 WHERE acknowledged=0")
    conn().commit()
    return cur.rowcount


def alert_exists_recently(product_id: str, store: str, price_cents: int,
                          window_s: int = 3 * 86400) -> bool:
    """Evita repetir o mesmo alerta todo dia enquanto a promo dura."""
    row = conn().execute(
        "SELECT 1 FROM alerts WHERE product_id=? AND store=? AND price_cents<=? AND ts>=?"
        " LIMIT 1", (product_id, store, price_cents, now() - window_s)).fetchone()
    return row is not None
