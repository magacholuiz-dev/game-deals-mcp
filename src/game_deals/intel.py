"""Price intelligence: what is the best price right now, is it a good one, should
I buy or wait, and how do I spend a budget.

Everything here is a rule that can be read and tested. There is no opaque model:
a wrong "buy now" costs real money, so each decision carries its reasons and an
uncertainty figure, and when the data cannot support a claim the answer is
"neutro" or "sem base", never a confident guess.

Money is integer cents throughout. Floats appear only in scores and ratios.
"""
from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import asdict, dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from . import db, events, providers, rhythm
from .matching import anchors, has_anchors, normalize
from .models import brl
from .verdict import _humanize, evaluate

DAY = 86400

# ------------------------------------------------------------------ freshness

FRESH_FLOOR_S = 3 * DAY
FRESH_CADENCES = 3


def fresh_limit_s(source: str) -> int:
    """How old an observation may be and still count as a price you can act on:
    three of the source's own cadences, never less than three days. A weekly
    source is not declared stale after a week."""
    return max(FRESH_FLOOR_S, FRESH_CADENCES * rhythm.base_cadence(source))


@dataclass
class StoreQuote:
    store: str
    source: str
    price_cents: int
    regular_cents: int | None
    in_stock: bool
    url: str
    observed_ts: int
    age_s: int
    fresh: bool

    @property
    def discount_pct(self) -> int:
        if not self.regular_cents or self.regular_cents <= self.price_cents:
            return 0
        return round(100 * (1 - self.price_cents / self.regular_cents))

    def dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.update(preco=brl(self.price_cents), desconto_pct=self.discount_pct,
                 idade_horas=round(self.age_s / 3600, 1))
        return d


def latest_quotes(product_id: str, now: int) -> list[StoreQuote]:
    """The most recent reading of each store, official stores only. Community
    signals are never in price_points, so they cannot show up here."""
    rows = db.conn().execute(
        "SELECT pp.* FROM price_points pp JOIN ("
        " SELECT store, MAX(ts) ts FROM price_points WHERE product_id=? GROUP BY store"
        ") m ON m.store=pp.store AND m.ts=pp.ts WHERE pp.product_id=?",
        (product_id, product_id)).fetchall()
    out = []
    for r in rows:
        if providers.kind(r["source"]) != "store":
            continue
        age = max(0, now - r["ts"])
        out.append(StoreQuote(r["store"], r["source"], r["price_cents"],
                              r["regular_cents"], bool(r["in_stock"]), r["url"],
                              r["ts"], age, age <= fresh_limit_s(r["source"])))
    return sorted(out, key=lambda q: q.price_cents)


# ------------------------------------------------- 1. aggregate verdict

@dataclass
class AggregateVerdict:
    product_id: str
    best: StoreQuote | None
    fresh: list[StoreQuote]
    stale: list[StoreQuote]
    label: str
    is_all_time_low: bool = False
    days_since_lower: int | None = None
    confidence: str = "baixa"
    confidence_reasons: list[str] = field(default_factory=list)
    spread_cents: int | None = None
    freshness_note: str = ""
    verdict: dict[str, Any] | None = None

    def dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["best"] = self.best.dict() if self.best else None
        d["fresh"] = [q.dict() for q in self.fresh]
        d["stale"] = [q.dict() for q in self.stale]
        return d


def veredito_agregado(product_id: str, now: int | None = None) -> AggregateVerdict:
    """The lowest CURRENT price across official stores, compared with every price
    ever seen in any store.

    Two different questions, deliberately kept apart. Which prices count as
    "available now" is filtered by freshness: a six-month-old listing of a store
    that stopped being collected must not be presented as an active deal. Which
    prices count as HISTORY is not filtered: a real price observed a year ago is
    still a real price, and dropping it would make today look better than it is.
    """
    now = now or db.now()
    quotes = latest_quotes(product_id, now)
    fresh = [q for q in quotes if q.fresh]
    stale = [q for q in quotes if not q.fresh]
    note = ""
    if stale:
        parts = ", ".join(f"{q.store} (há {_humanize(max(1, q.age_s // DAY))})" for q in stale)
        note = f"preço antigo ignorado: {parts}"

    buyable = [q for q in fresh if q.in_stock]
    if not buyable:
        why = ("nenhuma loja com preço recente" if not fresh
               else "nenhuma loja com estoque")
        return AggregateVerdict(product_id, None, fresh, stale,
                                f"sem preço atual: {why}", freshness_note=note)

    best = buyable[0]
    spread = (buyable[1].price_cents - best.price_cents) if len(buyable) > 1 else None
    v = evaluate(product_id, best.store, best.price_cents, scope_store=False, now=now)

    if v.samples <= 1:
        label = "sem histórico ainda (primeira leitura)"
    elif v.is_all_time_low and v.enough_history:
        label = f"menor preço já registrado em todas as lojas ({v.history_days} dias de coleta)"
    elif v.is_all_time_low:
        label = v.label                       # "primeiras leituras..."
    elif v.above_recent_min:
        label = v.label                       # "não é promoção — esteve a ..."
    elif v.days_since_lower is not None and v.days_since_lower > 0:
        label = f"menor preço registrado em todas as lojas em {_humanize(v.days_since_lower)}"
    else:
        label = v.label
    return AggregateVerdict(
        product_id, best, fresh, stale, label, v.is_all_time_low and v.enough_history,
        v.days_since_lower, v.confidence, v.confidence_reasons, spread, note, v.dict())


# ---------------------------------------- typical discount depth (publisher)

MIN_PRODUCTS_FOR_TYPICAL = 3


def typical_discount_depth(platform: str = "", publisher: str = "",
                           exclude_product: str = "") -> dict[str, Any]:
    """How deep promotions usually go for comparable products.

    For each product take the deepest discount any store ever reported (so one
    long sale does not count many times), then the median over products. The
    publisher is used when at least MIN_PRODUCTS_FOR_TYPICAL of its products
    have a recorded discount, otherwise the platform, otherwise nothing. The
    answer always says which basis it used and how many products stand behind it.

    `exclude_product` keeps a product out of its own benchmark: deciding whether
    THIS discount is deep by comparing it with a median that already contains it
    would be circular.
    """
    def depth(where: str, args: list[Any]) -> list[float]:
        if exclude_product:
            where += " AND pp.product_id <> ?"
            args = args + [exclude_product]
        rows = db.conn().execute(
            "SELECT MAX(1.0 - pp.price_cents * 1.0 / pp.regular_cents) d "
            "FROM price_points pp JOIN products p ON p.id = pp.product_id "
            f"WHERE pp.regular_cents > pp.price_cents AND pp.regular_cents > 0 {where} "
            "GROUP BY pp.product_id", args).fetchall()
        return [r["d"] for r in rows]

    for basis, where, args in (
            ("publisher", "AND p.publisher = ? AND p.publisher <> ''", [publisher]),
            ("plataforma", "AND p.platform = ? AND p.platform <> ''", [platform])):
        if basis == "publisher" and not publisher:
            continue
        if basis == "plataforma" and not platform:
            continue
        ds = depth(where, args)
        if len(ds) >= MIN_PRODUCTS_FOR_TYPICAL:
            return {"pct": round(100 * statistics.median(ds)), "base": basis,
                    "produtos": len(ds)}
    return {"pct": None, "base": "nenhuma", "produtos": 0}


# --------------------------------------------- 2. buy now or wait

SHALLOW_ABS_PCT = 15         # "shallow" when nothing typical is known
SHALLOW_REL = 0.5            # shallow = under half of the typical depth
EVENT_WINDOW_DAYS = 30
COMPRE_AGORA, ESPERE, NEUTRO = "compre_agora", "espere", "neutro"

_EVENT_UNCERTAINTY = {events.RULE: 0.0, events.REPORTED: 0.10, events.ESTIMATED: 0.25}


@dataclass
class Recomendacao:
    product_id: str
    decisao: str                       # compre_agora | espere | neutro
    justificativa: list[str]
    incerteza: float                   # 0..1, higher = trust it less
    incerteza_texto: str
    fatores: dict[str, Any] = field(default_factory=dict)

    def dict(self) -> dict[str, Any]:
        return asdict(self)


def _uncertainty_text(u: float) -> str:
    return "baixa" if u < 0.30 else "média" if u < 0.60 else "alta"


def _typical_price(product_id: str, now: int, days: int = 365) -> tuple[int | None, int]:
    """Median of the daily minimum price over the last year, and how many days
    it is based on. The typical price a buyer really saw, not a list price."""
    rows = db.conn().execute(
        "SELECT CAST(ts/86400 AS INT) d, MIN(price_cents) m FROM price_points "
        "WHERE product_id=? AND ts>=? GROUP BY d", (product_id, now - days * DAY)).fetchall()
    if len(rows) < 5:
        return None, len(rows)
    return int(statistics.median(r["m"] for r in rows)), len(rows)


def _platform_key(product: Any) -> str:
    return (product["platform"] or "").lower()


def recomendar_compra(product_id: str, today: dt.date | None = None,
                      now: int | None = None) -> Recomendacao:
    now = now or db.now()
    today = today or dt.date.fromtimestamp(now)
    p = db.get_product(product_id)
    if p is None:
        raise KeyError(f"unknown product {product_id!r}")
    agg = veredito_agregado(product_id, now)
    facts: dict[str, Any] = {"produto": p["title"]}
    why: list[str] = []

    # 1. Not released: a pre-order never goes on sale.
    if p["released"]:
        try:
            release = dt.date.fromisoformat(p["released"][:10])
        except ValueError:
            release = None
        if release and release > today:
            facts["lancamento"] = release.isoformat()
            why.append(f"ainda não foi lançado ({release:%d/%m/%Y}): pré-venda não entra "
                       "em promoção, o alerta útil é o de queda de preço")
            return Recomendacao(product_id, NEUTRO, why, 0.20, "baixa", facts)

    # 2. Nothing current to judge.
    if agg.best is None:
        why.append(agg.label)
        if agg.freshness_note:
            why.append(agg.freshness_note)
        return Recomendacao(product_id, NEUTRO, why, 1.0, "alta", facts)

    best = agg.best
    typical_price, typical_days = _typical_price(product_id, now)
    if typical_price:
        real_discount = max(0.0, 1 - best.price_cents / typical_price)
        facts["desconto_real_pct"] = round(100 * real_discount)
        facts["preco_tipico"] = brl(typical_price)
        discount_basis = "preço típico"
    else:
        real_discount = best.discount_pct / 100
        discount_basis = "preço cheio informado pela loja"
    facts.update(preco=brl(best.price_cents), loja=best.store,
                 desconto_loja_pct=best.discount_pct,
                 confianca_do_veredito=agg.confidence)
    typ = typical_discount_depth(_platform_key(p), p["publisher"] or "", product_id)
    facts["desconto_tipico"] = typ

    u = 0.15
    if agg.confidence == "baixa":
        u += 0.35
    elif agg.confidence == "media":
        u += 0.15
    if typ["pct"] is None:
        u += 0.15
    if len(agg.fresh) == 1:
        u += 0.05
    if typical_price is None:
        u += 0.10

    # 3. A major sale is close and today's discount is shallow.
    platform = _platform_key(p)
    coming = [e for e in events.upcoming(today, EVENT_WINDOW_DAYS,
                                         platform=platform or None,
                                         magnitude=events.MAJOR)
              if e.days_until(today) > 0]
    shallow_limit = (SHALLOW_REL * typ["pct"] / 100) if typ["pct"] is not None \
        else SHALLOW_ABS_PCT / 100
    if coming and real_discount < shallow_limit:
        e = coming[0]
        d = e.days_until(today)
        cert = {"rule": "data calculada por regra", "reported": "data reportada, não confirmada",
                "estimated": "data estimada, ainda não anunciada"}[e.certainty]
        why.append(f"{e.name} começa em {d} dias ({e.start:%d/%m}; {cert})")
        base = (f"típico de {typ['pct']}% para {typ['base']} ({typ['produtos']} produtos)"
                if typ["pct"] is not None else f"abaixo de {SHALLOW_ABS_PCT}%, sem base de desconto típico")
        why.append(f"o desconto atual sobre o {discount_basis} é de "
                   f"{round(100 * real_discount)}%, raso frente ao {base}")
        facts["evento"] = {"nome": e.name, "dias": d, "certeza": e.certainty}
        u += _EVENT_UNCERTAINTY[e.certainty]
        return Recomendacao(product_id, ESPERE, why, min(1, round(u, 2)),
                            _uncertainty_text(u), facts)

    # 4. The best price ever recorded, with history to back the claim. It ranks
    # BELOW the event rule on purpose: a low reached with a shallow discount, days
    # before a big sale, is a weak low.
    if agg.is_all_time_low and agg.confidence != "baixa":
        why.append(agg.label)
        return Recomendacao(product_id, COMPRE_AGORA, why, min(1, round(u, 2)),
                            _uncertainty_text(u), facts)

    # 5. The discount already matches what promotions usually reach.
    if typ["pct"] is not None and real_discount * 100 >= typ["pct"]:
        why.append(f"desconto de {round(100 * real_discount)}% sobre o {discount_basis} "
                   f"iguala ou supera o típico de {typ['pct']}% para {typ['base']} "
                   f"({typ['produtos']} produtos)")
        return Recomendacao(product_id, COMPRE_AGORA, why, min(1, round(u, 2)),
                            _uncertainty_text(u), facts)

    # 6. Not a promotion: it has been cheaper recently.
    v = agg.verdict or {}
    if v.get("above_recent_min"):
        why.append(agg.label)
        if typ["pct"] is not None:
            why.append(f"promoções desse tipo chegam a {typ['pct']}% (base: {typ['base']})")
        return Recomendacao(product_id, ESPERE, why, min(1, round(u, 2)),
                            _uncertainty_text(u), facts)

    why.append("sem sinal claro: nem é o menor preço, nem há promoção grande à vista, "
               "nem o desconto atinge o típico")
    return Recomendacao(product_id, NEUTRO, why, min(1, round(u, 2)),
                        _uncertainty_text(u), facts)
