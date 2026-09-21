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


# ------------------------------------------------- 3. opportunity score

WEIGHTS = {"desconto": 0.35, "nota": 0.25, "popularidade": 0.10,
           "valor_hora": 0.20, "prioridade": 0.10}
FULL_MARKS_DISCOUNT = 0.50          # 50% under the typical price = full marks
PRICE_PER_HOUR_GOOD = 5.0           # R$ per hour of play: full marks at or below
PRICE_PER_HOUR_POOR = 20.0          # and none at or above
PRIORITY_VALUE = {0: 0.0, 1: 0.6, 2: 1.0}
POP_CEILING_LOG = 4.0               # same scale as ratings.relevancia


@dataclass
class Oportunidade:
    product_id: str
    score: float | None
    preco_cents: int | None
    componentes: dict[str, dict[str, float]]
    cobertura: float                 # share of the weight that had data
    referencia_preco: str
    preco_por_hora: float | None
    notas: list[str]

    def dict(self) -> dict[str, Any]:
        return asdict(self)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def pontuar_oportunidade(product_id: str, now: int | None = None) -> Oportunidade:
    """0 to 100. Missing inputs are left out and the remaining weights are
    renormalized, and `cobertura` says how much of the score is backed by data.
    The discount is measured against the TYPICAL price the buyer actually saw
    (median of the daily minimum over a year), not the list price a store prints,
    which can be inflated."""
    now = now or db.now()
    p = db.get_product(product_id)
    if p is None:
        raise KeyError(f"unknown product {product_id!r}")
    agg = veredito_agregado(product_id, now)
    if agg.best is None:
        return Oportunidade(product_id, None, None, {}, 0.0, "nenhuma", None,
                            [agg.label])

    price = agg.best.price_cents
    notes: list[str] = []
    comps: dict[str, float] = {}

    typical, days = _typical_price(product_id, now)
    if typical:
        comps["desconto"] = _clamp01(max(0.0, 1 - price / typical) / FULL_MARKS_DISCOUNT)
        ref = f"preço típico ({brl(typical)}, {days} dias com leitura)"
    elif agg.best.regular_cents and agg.best.regular_cents > price:
        comps["desconto"] = _clamp01((1 - price / agg.best.regular_cents) / FULL_MARKS_DISCOUNT)
        ref = "preço cheio informado pela loja"
        notes.append("histórico curto: o desconto usa o preço cheio da loja, que pode estar inflado")
    else:
        ref = "sem referência de preço"
        notes.append("sem histórico nem preço cheio para medir o desconto")

    if p["metacritic"] is not None:
        comps["nota"] = _clamp01(p["metacritic"] / 100)
    elif p["user_rating"]:
        comps["nota"] = _clamp01(p["user_rating"] / 5)
    else:
        notes.append("sem nota da crítica")
    if p["popularity"]:
        comps["popularidade"] = _clamp01(math.log10(1 + p["popularity"]) / POP_CEILING_LOG)

    per_hour = None
    if p["playtime_hours"]:
        per_hour = round(price / 100 / p["playtime_hours"], 2)
        comps["valor_hora"] = _clamp01((PRICE_PER_HOUR_POOR - per_hour)
                                       / (PRICE_PER_HOUR_POOR - PRICE_PER_HOUR_GOOD))
    else:
        notes.append("sem horas de jogo: valor por hora não avaliado")
    comps["prioridade"] = PRIORITY_VALUE[p["priority"] or 0]

    used = {k: WEIGHTS[k] for k in comps}
    total_w = sum(used.values())
    score = round(100 * sum(WEIGHTS[k] * v for k, v in comps.items()) / total_w, 1)
    coverage = round(total_w / sum(WEIGHTS.values()), 2)
    if coverage < 0.5:
        notes.append(f"pouca base: só {int(coverage * 100)}% do peso tem dados")
    detail = {k: {"valor": round(v, 3), "peso": WEIGHTS[k],
                  "contribuicao": round(100 * WEIGHTS[k] * v / total_w, 1)}
              for k, v in comps.items()}
    return Oportunidade(product_id, score, price, detail, coverage, ref, per_hour, notes)


# ------------------------------------------------------ budget planner

def to_cents(valor: Any) -> int:
    """BRL amount to integer cents without going through binary floating point.

    `Decimal(str(x))` reads 300.1 as exactly 300.10, where 300.1 * 100 is
    30009.999999999996 and would silently lose a cent (or overshoot one)."""
    try:
        d = Decimal(str(valor).replace(",", "."))
    except InvalidOperation as e:
        raise ValueError(f"not a money amount: {valor!r}") from e
    if not d.is_finite() or d < 0:
        raise ValueError(f"budget must be a finite, non-negative amount: {valor!r}")
    return int((d * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


@dataclass
class ItemPlano:
    product_id: str
    titulo: str
    preco_cents: int
    score: float
    loja: str
    decisao: str

    def dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["preco"] = brl(self.preco_cents)
        return d


@dataclass
class Plano:
    limite_cents: int
    gasto_cents: int
    itens: list[ItemPlano]
    adiados: list[dict[str, Any]]
    sem_preco: list[str]
    nao_cabem: list[dict[str, Any]]
    score_total: float
    guloso: dict[str, Any]
    metodo: str = "exato (programação dinâmica sobre centavos)"

    @property
    def sobra_cents(self) -> int:
        return self.limite_cents - self.gasto_cents

    def dict(self) -> dict[str, Any]:
        return {"limite": brl(self.limite_cents), "limite_cents": self.limite_cents,
                "gasto": brl(self.gasto_cents), "gasto_cents": self.gasto_cents,
                "sobra": brl(self.sobra_cents), "sobra_cents": self.sobra_cents,
                "itens": [i.dict() for i in self.itens], "adiados": self.adiados,
                "sem_preco": self.sem_preco, "nao_cabem": self.nao_cabem,
                "score_total": self.score_total, "guloso": self.guloso,
                "metodo": self.metodo}


def melhor_combinacao(items: list[tuple[int, int]], budget: int) -> list[int]:
    """Exact 0/1 knapsack over integer cents: indices of the subset with the
    highest total value whose total price fits `budget`.

    `items` is [(price_cents, value_milli)], both integers, so nothing depends on
    floating point. The state set keeps only Pareto-optimal (cost, value) pairs,
    which stays small for a wishlist. Ties on value go to the cheaper subset, so
    a plan never spends money it does not have to."""
    states: list[tuple[int, int, tuple[int, ...]]] = [(0, 0, ())]   # cost, value, picks
    for idx, (price, value) in enumerate(items):
        if price > budget or value <= 0:
            continue
        grown = [(c + price, v + value, pk + (idx,)) for c, v, pk in states
                 if c + price <= budget]
        merged = sorted(states + grown, key=lambda s: (s[0], -s[1]))
        pruned: list[tuple[int, int, tuple[int, ...]]] = []
        best_value = -1
        for c, v, pk in merged:
            if v > best_value:               # dearer states must be worth more
                pruned.append((c, v, pk))
                best_value = v
        states = pruned
    return list(max(states, key=lambda s: (s[1], -s[0]))[2])


def _greedy(items: list[tuple[int, int]], budget: int) -> list[int]:
    order = sorted(range(len(items)), key=lambda i: (-items[i][1] / max(1, items[i][0]), i))
    spent, picks = 0, []
    for i in order:
        if items[i][1] > 0 and spent + items[i][0] <= budget:
            spent += items[i][0]
            picks.append(i)
    return picks


def planejar_orcamento(limite_reais: Any, itens_desejados: list[str] | None = None, *,
                       respeitar_espere: bool = True, score_minimo: float = 0.0,
                       now: int | None = None, today: dt.date | None = None) -> Plano:
    """Pick what to buy with a monthly budget.

    Maximizes the summed opportunity score, exactly, with integer cents. By
    default a game whose recommendation is "espere" is held back and listed with
    the reason, since spending the budget on something a sale is about to
    cheapen is a worse plan. Items with no current price are listed apart."""
    now = now or db.now()
    today = today or dt.date.fromtimestamp(now)
    budget = to_cents(limite_reais)
    ids = itens_desejados if itens_desejados is not None else \
        sorted({w["product_id"] for w in db.active_watches()})

    candidates: list[tuple[str, str, int, float, str, str]] = []
    adiados: list[dict[str, Any]] = []
    sem_preco: list[str] = []
    nao_cabem: list[dict[str, Any]] = []
    for pid in ids:
        p = db.get_product(pid)
        if p is None:
            continue
        op = pontuar_oportunidade(pid, now)
        if op.score is None or op.preco_cents is None:
            sem_preco.append(pid)
            continue
        rec = recomendar_compra(pid, today, now)
        if respeitar_espere and rec.decisao == ESPERE:
            adiados.append({"product_id": pid, "titulo": p["title"],
                            "motivo": rec.justificativa[0] if rec.justificativa else ""})
            continue
        if op.score < score_minimo:
            continue
        if op.preco_cents > budget:
            nao_cabem.append({"product_id": pid, "titulo": p["title"],
                              "preco": brl(op.preco_cents)})
            continue
        agg = veredito_agregado(pid, now)
        candidates.append((pid, p["title"], op.preco_cents, op.score,
                           agg.best.store if agg.best else "", rec.decisao))

    candidates.sort(key=lambda c: c[0])                    # deterministic ties
    vals = [(c[2], int(round(c[3] * 1000))) for c in candidates]
    picks = melhor_combinacao(vals, budget)
    chosen = [candidates[i] for i in sorted(picks, key=lambda i: -vals[i][1])]
    spent = sum(c[2] for c in chosen)
    assert spent <= budget, "plan exceeds the budget"     # integer arithmetic: cannot happen

    greedy = _greedy(vals, budget)
    g_spent = sum(vals[i][0] for i in greedy)
    assert g_spent <= budget
    total = round(sum(vals[i][1] for i in picks) / 1000, 1)
    return Plano(
        budget, spent,
        [ItemPlano(c[0], c[1], c[2], c[3], c[4], c[5]) for c in chosen],
        adiados, sem_preco, nao_cabem, total,
        {"score_total": round(sum(vals[i][1] for i in greedy) / 1000, 1),
         "gasto_cents": g_spent,
         "itens": [candidates[i][0] for i in greedy]})


# ------------------------------------------- 4. editions and media comparison

EDITION_ORDER = ("standard", "deluxe", "ultimate", "special")
SMALL_MARKUP_PCT = 15
EDITION_NOTICE = ("O conteúdo bônus não é avaliado: a comparação usa só preço e "
                  "histórico. Se o extra te interessa, isso pesa mais que a conta.")


def edition_of(product_id: str) -> str:
    """standard | deluxe | ultimate | special. A `#edition` on a store alias is
    exact (see the PlayStation provider); otherwise the title decides, and a game
    with no edition marker is the standard one."""
    for a in db.aliases_for(product_id):
        if "#" in a["source_id"]:
            tier = a["source_id"].split("#", 1)[1].lower()
            if tier in EDITION_ORDER:
                return tier
    p = db.get_product(product_id)
    t = normalize(p["title"]) if p else ""
    if "ultimate" in t:
        return "ultimate"
    if "deluxe" in t:
        return "deluxe"
    if any(w in t.split() for w in ("collector", "colecionador", "especial", "special")):
        return "special"
    return "standard"


def _min_and_depth(product_id: str) -> tuple[int | None, int | None]:
    r = db.conn().execute(
        "SELECT MIN(price_cents) m, MAX(CASE WHEN regular_cents > price_cents "
        "THEN 1.0 - price_cents * 1.0 / regular_cents END) d "
        "FROM price_points WHERE product_id=?", (product_id,)).fetchone()
    return r["m"], (round(100 * r["d"]) if r["d"] is not None else None)


def comprar_edicoes(produtos_id: list[str], now: int | None = None) -> dict[str, Any]:
    """Standard vs Deluxe vs Ultimate of the same game, priced and compared with
    each edition's own history."""
    now = now or db.now()
    rows: list[dict[str, Any]] = []
    avisos = [EDITION_NOTICE]
    for pid in produtos_id:
        p = db.get_product(pid)
        if p is None:
            avisos.append(f"produto desconhecido: {pid}")
            continue
        agg = veredito_agregado(pid, now)
        lo, depth = _min_and_depth(pid)
        rows.append({"product_id": pid, "titulo": p["title"], "edicao": edition_of(pid),
                     "preco_cents": agg.best.price_cents if agg.best else None,
                     "loja": agg.best.store if agg.best else None,
                     "minimo_historico_cents": lo, "maior_desconto_pct": depth,
                     "veredito": agg.label})
    rows.sort(key=lambda r: (EDITION_ORDER.index(r["edicao"]), r["preco_cents"] or 10**12))
    priced = [r for r in rows if r["preco_cents"] is not None]
    for r in rows:
        r["preco"] = brl(r["preco_cents"]) if r["preco_cents"] is not None else None
        r["minimo_historico"] = brl(r["minimo_historico_cents"])
    comparacoes: list[dict[str, Any]] = []
    if len(priced) < 2:
        avisos.append("preciso de preço atual de pelo menos duas edições para comparar")
        return {"edicoes": rows, "comparacoes": comparacoes, "avisos": avisos}

    base = priced[0]
    base_p = db.get_product(base["product_id"])
    base_anchor = anchors(base_p["title"])
    for r in priced[1:]:
        title = db.get_product(r["product_id"])["title"]
        if not has_anchors(title, base_anchor):
            avisos.append(f"{title!r} não parece ser do mesmo jogo que {base_p['title']!r}")
        diff = r["preco_cents"] - base["preco_cents"]
        pct = round(100 * diff / base["preco_cents"])
        if diff < 0:
            veredito = (f"anomalia: a edição {r['edicao']} está mais barata que a "
                        f"{base['edicao']}; confira se são a mesma loja e o mesmo produto")
        elif r["minimo_historico_cents"] is not None and \
                r["minimo_historico_cents"] <= base["preco_cents"]:
            veredito = (f"a {r['edicao']} já custou {brl(r['minimo_historico_cents'])}, no nível "
                        f"da {base['edicao']} hoje ({base['preco']}): vale esperar essa queda")
        elif pct <= SMALL_MARKUP_PCT:
            veredito = (f"diferença pequena (+{pct}%): se o extra te interessa, "
                        "tende a valer levar a edição maior")
        else:
            extra = (f"; a {r['edicao']} já chegou a {r['maior_desconto_pct']}% de desconto"
                     if r["maior_desconto_pct"] else "")
            veredito = f"acréscimo de {pct}% ({brl(diff)}){extra}"
        comparacoes.append({"de": base["edicao"], "para": r["edicao"],
                            "diferenca_cents": diff, "diferenca": brl(diff),
                            "diferenca_pct": pct, "veredito": veredito})
    return {"edicoes": rows, "comparacoes": comparacoes, "avisos": avisos}


def comparar_upgrade_switch2(base_switch1_id: str, upgrade_pack_id: str,
                             switch2_full_id: str, possui_base: bool = False,
                             now: int | None = None) -> dict[str, Any]:
    """Is the Switch 1 game plus the paid Switch 2 upgrade pack cheaper than the
    full Switch 2 Edition? If you already own the base game, only the pack counts."""
    from .providers import nintendo
    now = now or db.now()
    avisos: list[str] = []

    def cur(pid: str) -> tuple[int | None, str]:
        p = db.get_product(pid)
        title = p["title"] if p else pid
        agg = veredito_agregado(pid, now) if p else None
        return (agg.best.price_cents if agg and agg.best else None), title

    base_c, base_t = cur(base_switch1_id)
    up_c, up_t = cur(upgrade_pack_id)
    full_c, full_t = cur(switch2_full_id)
    if not nintendo.classify(up_t).upgrade_pack:
        avisos.append(f"{up_t!r} não parece ser um Upgrade Pack (o título não diz isso)")
    if nintendo.classify(full_t).tier not in (nintendo.EDICAO, nintendo.NATIVO):
        avisos.append(f"{full_t!r} não parece ser uma edição de Switch 2")

    def lo(pid: str) -> int | None:
        return _min_and_depth(pid)[0]

    path_a_parts = [("Upgrade Pack", up_c)] if possui_base else \
        [("jogo de Switch 1", base_c), ("Upgrade Pack", up_c)]
    a_cost = None if any(c is None for _, c in path_a_parts) else sum(c for _, c in path_a_parts)
    caminhos = [
        {"nome": "só o Upgrade Pack (você já tem o jogo)" if possui_base
         else "jogo de Switch 1 + Upgrade Pack", "custo_cents": a_cost,
         "custo": brl(a_cost) if a_cost is not None else None,
         "itens": [{"item": n, "preco": brl(c) if c is not None else None}
                   for n, c in path_a_parts]},
        {"nome": "edição de Switch 2 completa", "custo_cents": full_c,
         "custo": brl(full_c) if full_c is not None else None,
         "itens": [{"item": "edição completa", "preco": brl(full_c) if full_c is not None else None}]}]
    if a_cost is None or full_c is None:
        avisos.append("falta preço atual de algum item: não dá para dizer qual caminho é mais barato")
        return {"caminhos": caminhos, "mais_barato": None, "economia_cents": None,
                "avisos": avisos}

    cheaper = caminhos[0] if a_cost <= full_c else caminhos[1]
    saving = abs(a_cost - full_c)
    hist_parts = [lo(upgrade_pack_id)] + ([] if possui_base else [lo(base_switch1_id)])
    hist_a = None if any(h is None for h in hist_parts) else sum(hist_parts)
    hist_b = lo(switch2_full_id)
    extra: dict[str, Any] = {}
    if hist_a is not None and hist_b is not None:
        extra["minimos_historicos"] = {
            "caminho_a": brl(hist_a), "caminho_b": brl(hist_b),
            "nota": "soma dos menores preços de cada peça, sem garantir que caiam juntas"}
    return {"caminhos": caminhos, "mais_barato": cheaper["nome"], "economia_cents": saving,
            "economia": brl(saving), "avisos": avisos, **extra}


PHYSICAL = ("midia fisica", "fisica", "disco", "cartucho", "blu ray")
DIGITAL_MARK = ("digital", "codigo", "code in box", "key", "download")
SIGNAL_FRESH_S = 7 * DAY


def comparar_midia(product_id: str, now: int | None = None) -> dict[str, Any]:
    """Physical retail against the digital store. Physical prices come from
    community offers, which are posts by users: they are marked as such, must be
    recent, and do not include shipping."""
    now = now or db.now()
    agg = veredito_agregado(product_id, now)
    digital = None
    if agg.best:
        digital = {"preco_cents": agg.best.price_cents, "preco": brl(agg.best.price_cents),
                   "loja": agg.best.store, "url": agg.best.url}

    fisica = None
    for r in db.signals_for(product_id, 50):
        t = normalize(r["title"])
        if not any(p in t for p in PHYSICAL) or any(d in t for d in DIGITAL_MARK):
            continue
        if now - r["seen_ts"] > SIGNAL_FRESH_S:
            continue
        if fisica is None or r["price_cents"] < fisica["preco_cents"]:
            fisica = {"preco_cents": r["price_cents"], "preco": brl(r["price_cents"]),
                      "loja": r["store"], "url": r["url"], "titulo": r["title"]}

    avisos = ["preço de mídia física é oferta postada por usuário: confira antes de comprar",
              "o frete não está incluído"]
    if digital is None or fisica is None:
        falta = "digital" if digital is None else "física"
        return {"digital": digital, "fisica": fisica, "mais_barata": None,
                "diferenca_cents": None,
                "avisos": avisos + [f"sem preço recente de mídia {falta}: não dá para comparar"]}
    diff = digital["preco_cents"] - fisica["preco_cents"]
    winner = "fisica" if diff > 0 else "digital" if diff < 0 else "empate"
    return {"digital": digital, "fisica": fisica, "mais_barata": winner,
            "diferenca_cents": abs(diff), "diferenca": brl(abs(diff)), "avisos": avisos}
