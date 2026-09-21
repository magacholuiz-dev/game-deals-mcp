"""O veredito historico.

Nao perguntamos "e o menor preco dos ultimos 90 dias?" -- 90 e arbitrario.
Invertemos: *quanto tempo preciso voltar para achar um preco menor?*
Uma consulta so, e a resposta ja sai na linguagem que voce queria.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Any

from . import db, rhythm
from .models import brl

DAY = 86400


@dataclass
class Verdict:
    label: str
    is_all_time_low: bool
    days_since_lower: int | None      # None = nunca esteve mais barato
    min_90d_cents: int | None
    min_365d_cents: int | None
    min_all_cents: int | None
    samples: int
    history_days: int
    confidence: str                   # alta | media | baixa
    above_recent_min: bool = False    # preco atual acima do minimo de 90d
    enough_history: bool = False      # historico suficiente para alegar minimo
    caveat: str = ""
    # Why the confidence is what it is, and what it was before collection gaps
    # were taken into account. `gap` is None when there was nothing to judge.
    confidence_before_gaps: str = ""
    confidence_reasons: list[str] = field(default_factory=list)
    gap: dict[str, Any] | None = None

    def dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["min_90d"] = brl(self.min_90d_cents)
        d["min_365d"] = brl(self.min_365d_cents)
        d["min_all"] = brl(self.min_all_cents)
        return d


def _humanize(days: int) -> str:
    if days < 45:
        return f"{days} dias"
    if days < 365:
        return f"{round(days / 30)} meses"
    anos, resto = divmod(days, 365)
    meses = round(resto / 30)
    if meses == 0:
        return f"{anos} ano" if anos == 1 else f"{anos} anos"
    base = "1 ano" if anos == 1 else f"{anos} anos"
    # o plural de "mês" perde o circunflexo
    return f"{base} e {meses} " + ("mês" if meses == 1 else "meses")


LEVELS = ("baixa", "media", "alta")
_DROP = {"none": 0, "minor": 1, "major": 2}


def _source_of(c, product_id: str, store: str, scope_store: bool) -> str:
    if scope_store:
        r = c.execute("SELECT source FROM price_points WHERE product_id=? AND store=? "
                      "ORDER BY ts DESC LIMIT 1", (product_id, store)).fetchone()
    else:
        r = c.execute("SELECT source FROM price_points WHERE product_id=? "
                      "ORDER BY ts DESC LIMIT 1", (product_id,)).fetchone()
    return r["source"] if r else ""


def apply_gaps(confidence: str, report: "rhythm.GapReport | None") -> tuple[str, str]:
    """(new confidence, reason or ''). A minor gap costs one level, a major gap
    two, never below "baixa". Nothing changes when there is nothing to judge."""
    if report is None or report.severity == "none" or confidence == "baixa":
        return confidence, ""
    idx = max(0, LEVELS.index(confidence) - _DROP[report.severity])
    return LEVELS[idx], report.text()


def evaluate(product_id: str, store: str, price_cents: int,
             scope_store: bool = True, now: int | None = None) -> Verdict:
    """scope_store=True compara so contra a mesma loja (mais honesto);
    False compara contra o melhor preco visto em qualquer loja.

    `now` exists so tests can pin the clock. The confidence also looks at the
    collection rhythm recorded in job_runs (see rhythm.py)."""
    now = now or db.now()
    c = db.conn()
    where = "product_id=?" + (" AND store=?" if scope_store else "")
    args_base: list[Any] = [product_id] + ([store] if scope_store else [])

    row = c.execute(
        f"SELECT MAX(ts) AS t FROM price_points WHERE {where} AND price_cents<?",
        (*args_base, price_cents)).fetchone()
    last_lower_ts = row["t"] if row else None

    stats = c.execute(
        f"SELECT COUNT(*) AS n, MIN(ts) AS first_ts, MIN(price_cents) AS min_all "
        f"FROM price_points WHERE {where}", args_base).fetchone()
    samples = stats["n"] or 0
    first_ts = stats["first_ts"]
    min_all = stats["min_all"]

    def _min_since(days: int) -> int | None:
        r = c.execute(
            f"SELECT MIN(price_cents) AS m FROM price_points WHERE {where} AND ts>=?",
            (*args_base, now - days * DAY)).fetchone()
        return r["m"] if r else None

    min_90 = _min_since(90)
    min_365 = _min_since(365)

    history_days = int((now - first_ts) / DAY) if first_ts else 0
    days_since_lower = (
        None if last_lower_ts is None else max(0, int((now - last_lower_ts) / DAY))
    )

    # "Menor preco ja registrado" com 2 leituras de ontem e tecnicamente verdade
    # e completamente vazio. Alegar minimo historico exige historico: um mes e
    # meia duzia de leituras e o piso minimo para a frase significar algo.
    historico_ok = samples >= 5 and history_days >= 30

    # Cold start e real: sem historico, o veredito nao vale nada. Diga isso.
    if samples <= 1:
        confidence = "baixa"
    elif history_days < 60 or samples < 10:
        confidence = "media"
    else:
        confidence = "alta"

    confidence_before = confidence
    reasons: list[str] = []
    if confidence != "alta" and samples > 1:
        reasons.append(f"histórico curto: {samples} leituras em {history_days} dias")

    # Collection rhythm over the period the claim covers. "Lowest in 7 months" is
    # judged over 7 months of runs, not over the last week.
    claim_days = days_since_lower if days_since_lower is not None else history_days
    gap_report = rhythm.assess_rhythm(
        _source_of(c, product_id, store, scope_store),
        window_days=min(365, max(14, claim_days)), now=now)
    confidence, gap_reason = apply_gaps(confidence, gap_report)
    if gap_reason:
        reasons.append(gap_reason)

    is_atl = days_since_lower is None and samples > 1

    # Armadilha: "menor preco dos ultimos N dias" e tecnicamente verdade sempre
    # que N e a distancia ate a ultima leitura menor -- inclusive quando o preco
    # SUBIU e a leitura de hoje e a unica da janela. Isso soa como promocao e
    # nao e. So usamos essa frase quando o preco atual bate ou fica abaixo do
    # minimo recente; caso contrario dizemos claramente que nao e promocao.
    acima_do_minimo_recente = min_90 is not None and price_cents > min_90

    if samples <= 1:
        label = "sem histórico ainda (primeira leitura)"
    elif is_atl and not historico_ok:
        # Mesma guarda do selo e do alerta: sem base, a frase nao pode soar como
        # conquista. "0 dias de coleta" e o oposto de uma boa noticia.
        label = (f"primeiras leituras ({samples}) — sem base de comparação ainda")
    elif is_atl:
        label = f"menor preço já registrado em {history_days} dias de coleta"
    elif acima_do_minimo_recente:
        quando = ""
        r = c.execute(
            f"SELECT MAX(ts) AS t FROM price_points WHERE {where} AND price_cents<=?"
            " AND ts>=?", (*args_base, min_90, now - 90 * DAY)).fetchone()
        if r and r["t"]:
            quando = f" há {_humanize(max(1, int((now - r['t']) / DAY)))}"
        label = f"não é promoção — esteve a {brl(min_90)}{quando}"
    elif days_since_lower == 0:
        label = "já esteve mais barato hoje"
    else:
        label = f"menor preço dos últimos {_humanize(days_since_lower)}"

    caveat = ""
    if confidence_before != "alta" and samples > 1:
        caveat = (f"apenas {samples} leituras em {history_days} dias — "
                  f"o veredito fica confiável depois de ~2 meses coletando")
    if gap_reason:
        caveat = (caveat + "; " if caveat else "") + gap_reason

    return Verdict(
        label=label,
        is_all_time_low=is_atl,
        days_since_lower=days_since_lower,
        min_90d_cents=min_90,
        min_365d_cents=min_365,
        min_all_cents=min_all,
        samples=samples,
        history_days=history_days,
        confidence=confidence,
        above_recent_min=bool(acima_do_minimo_recente),
        enough_history=historico_ok,
        caveat=caveat,
        confidence_before_gaps=confidence_before,
        confidence_reasons=reasons,
        gap=gap_report.dict() if gap_report else None,
    )


# --------------------------------------------------------------- selo

# Vive aqui, e nao no front, para que o dashboard e o MCP nunca discordem sobre
# o que e "destaque". Os limiares sao a unica coisa a mexer se voce achar o selo
# generoso ou avaro demais.
DESTAQUE_DIAS = 90        # a partir daqui vira DESTAQUE
BOM_DIAS = 30             # a partir daqui vira "bom preco"


def highlight(v: Verdict) -> dict | None:
    """Selo a exibir no card, ou None. Tres niveis, do mais forte ao mais fraco."""
    if v.samples <= 1 or v.confidence == "baixa":
        return None                      # sem historico nao existe destaque
    if v.above_recent_min:
        return None                      # preco acima do minimo recente nunca e destaque

    if v.is_all_time_low:
        # Sem historico suficiente a alegacao e vazia — e um selo dourado em
        # produto de ontem ensina voce a nao confiar no selo.
        if not v.enough_history:
            return None
        return {"tier": "historico", "texto": "MENOR PREÇO HISTÓRICO",
                "detalhe": f"em {v.history_days} dias de coleta"}

    d = v.days_since_lower or 0
    if d >= DESTAQUE_DIAS:
        return {"tier": "destaque", "texto": "DESTAQUE",
                "detalhe": f"menor preço em {_humanize(d)}"}
    if d >= BOM_DIAS:
        return {"tier": "bom", "texto": "BOM PREÇO",
                "detalhe": f"menor preço em {_humanize(d)}"}
    return None
