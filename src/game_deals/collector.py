"""Collection: fetch prices, record them, evaluate alerts, log every run.

Runs OUTSIDE the MCP (see scheduler.py). This is the process that gives the
project its value: without it the time series never grows and the verdict never
leaves "no history yet".

Structure
- `fetch_alias`     one network call, never touches the database (thread safe);
- `collect`         one job_run per SOURCE. Sources fetch in parallel (different
                    hosts, and the HTTP client already throttles per host); all
                    database writes happen afterwards on the calling thread;
- `collect_feed`    the community feed, logged the same way;
- `run`             the legacy one-shot used by the CLI and the MCP tool.
"""
from __future__ import annotations

import sys
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from . import alerts, db, jobs, providers
from .matching import price_band, within_band
from .models import Offer, brl
from .verdict import evaluate as verdict_for


# ------------------------------------------------------------------ fetching

@dataclass
class AliasResult:
    product_id: str
    source: str
    source_id: str
    offers: list[Offer] = field(default_factory=list)
    error: str = ""
    error_kind: str = ""
    latency_ms: int = 0


def fetch_alias(product_id: str, alias: Any) -> AliasResult:
    """Ask one provider about one alias. Pure network, no database access."""
    res = AliasResult(product_id, alias["source"], alias["source_id"])
    prov = providers.get(alias["source"])
    if prov is None or not prov.configured():
        res.error, res.error_kind = "provider not configured", "other"
        return res
    t0 = time.perf_counter()
    try:
        res.offers = prov.fetch(alias["source_id"])
        if not res.offers:
            res.error = getattr(prov, "last_error", "") or ""
            res.error_kind = jobs.classify_error(res.error)
    except Exception as e:                        # noqa: BLE001 - one bad alias
        res.error = f"{type(e).__name__}: {e}"    # must not stop the others
        res.error_kind = jobs.classify_error(res.error, e)
    res.latency_ms = int((time.perf_counter() - t0) * 1000)
    return res


def _write(res: AliasResult) -> None:
    for o in res.offers:
        db.record(res.product_id, o.source, o.store or o.source, o.price_cents,
                  o.regular_cents, o.currency, o.in_stock, o.url)
        db.set_image(res.product_id, o.image)


def refresh_product(product_id: str, quiet: bool = True) -> list[Offer]:
    """Every source with an alias for this product, recorded now."""
    offers: list[Offer] = []
    for alias in db.aliases_for(product_id):
        res = fetch_alias(product_id, alias)
        if res.error and not res.offers and not quiet:
            print(f"  ! {alias['source']}: {res.error}", file=sys.stderr)
        _write(res)
        offers.extend(res.offers)
    return offers


# ---------------------------------------------------------------------- plan

def plan(sources: list[str] | None = None) -> dict[str, list[tuple[str, Any]]]:
    """source -> [(product_id, alias)] for products someone is watching."""
    stores = providers.lojas()
    wanted = set(sources) if sources else set(stores)
    watched = {w["product_id"] for w in db.active_watches()}
    out: dict[str, list[tuple[str, Any]]] = {}
    for pid in sorted(watched):
        for alias in db.aliases_for(pid):
            if alias["source"] in stores and alias["source"] in wanted:
                out.setdefault(alias["source"], []).append((pid, alias))
    return out


# ---------------------------------------------------------------- run results

@dataclass
class SourceRun:
    source: str
    status: str = jobs.OK
    attempted: int = 0
    items: int = 0
    failed: int = 0
    latency_ms: int = 0
    error: str = ""
    error_kind: str = ""
    alerts: list[dict] = field(default_factory=list)
    run_id: int = 0


def _status(attempted: int, failed: int) -> str:
    if attempted == 0 or failed == 0:
        return jobs.OK
    return jobs.FAILED if failed >= attempted else jobs.PARTIAL


# --------------------------------------------------------------------- alerts

def _prev_by_store(product_id: str) -> dict[str, dict]:
    """The reading before this run: any_drop and back_in_stock compare to it."""
    out: dict[str, dict] = {}
    for row in db.conn().execute(
            "SELECT store, price_cents, in_stock, MAX(ts) AS ts FROM price_points "
            "WHERE product_id=? GROUP BY store", (product_id,)).fetchall():
        out[row["store"]] = dict(row)
    return out


def _evaluate_alerts(watches: list[Any], product_id: str, offers: list[Offer],
                     prev: dict[str, dict], verbose: bool) -> list[dict]:
    fired: list[dict] = []
    for w in watches:
        for o in offers:
            store = o.store or o.source
            v = verdict_for(product_id, store, o.price_cents)
            headline = alerts.evaluate(w["rule_kind"], w["rule_value"], o, v,
                                       prev.get(store))
            if not headline or db.alert_exists_recently(product_id, store, o.price_cents):
                continue
            msg = f"{store}: {headline} ({v.label})"
            db.add_alert(w["id"], product_id, o.source, store, o.price_cents, msg, o.url)
            alerts.notify(w["title"], msg, o.url)
            fired.append({"produto": w["title"], "loja": store,
                          "preco": brl(o.price_cents), "motivo": msg, "url": o.url})
            if verbose:
                print(f"  * ALERTA {msg}")
    return fired


# -------------------------------------------------------------------- collect

def _fetch_source(tasks: list[tuple[str, Any]]) -> list[AliasResult]:
    # Serial inside a source: same host, and the client throttles it anyway.
    return [fetch_alias(pid, alias) for pid, alias in tasks]


def collect(sources: list[str] | None = None, *, now: int | None = None,
            scheduled_for: dict[str, int] | None = None, verbose: bool = False,
            max_workers: int = 4) -> dict[str, SourceRun]:
    """One run per source, all logged in job_runs. Different sources fetch in
    parallel; every database write happens here, on the calling thread."""
    tasks = plan(sources)
    scheduled_for = scheduled_for or {}
    runs: dict[str, SourceRun] = {}
    for src in tasks:
        runs[src] = SourceRun(src, run_id=jobs.start(
            src, scheduled_for.get(src), now=now))

    fetched: dict[str, list[AliasResult]] = {}
    if tasks:
        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(tasks)))) as ex:
            futures = {src: ex.submit(_fetch_source, t) for src, t in tasks.items()}
            for src, fut in futures.items():
                try:
                    fetched[src] = fut.result()
                except Exception as e:            # noqa: BLE001
                    runs[src].error = f"{type(e).__name__}: {e}"
                    runs[src].error_kind = jobs.classify_error("", e)
                    fetched[src] = []

    watches_by_product: dict[str, list[Any]] = {}
    for w in db.active_watches():
        watches_by_product.setdefault(w["product_id"], []).append(w)

    for src, results in fetched.items():
        r = runs[src]
        r.attempted = len(tasks[src])
        for res in results:
            prev = _prev_by_store(res.product_id)          # BEFORE writing
            _write(res)
            r.items += len(res.offers)
            if res.error_kind in jobs.FAILURE_KINDS:
                r.failed += 1
                r.error = r.error or res.error
                r.error_kind = r.error_kind or res.error_kind
            r.latency_ms += res.latency_ms
            r.alerts += _evaluate_alerts(
                watches_by_product.get(res.product_id, []), res.product_id,
                res.offers, prev, verbose)
            if verbose:
                print(f"- {src}/{res.source_id}: {len(res.offers)} ofertas"
                      + (f"  ({res.error_kind}: {res.error[:60]})" if res.error else ""))
        if r.error_kind and not fetched[src]:
            r.failed = r.attempted
        r.latency_ms = r.latency_ms // max(1, len(results))
        r.status = _status(r.attempted, r.failed)
        jobs.finish(r.run_id, r.status, items=r.items, attempted=r.attempted,
                    failed=r.failed, latency_ms=r.latency_ms, error=r.error,
                    error_kind=r.error_kind, now=now)
    return runs


# ---------------------------------------------------------------- community

def filtrar_por_preco(sinais: list, teto_cents: int | None = None,
                      piso_cents: int | None = None) -> list:
    """Drop listings whose price cannot be the product itself.

    An explicit band (`teto`/`piso`, usually derived from a real store price)
    wins. Without one the median of the matched listings estimates the product
    and the cut is symmetric, see matching.price_band. Pure and unit tested."""
    if teto_cents or piso_cents:
        band = (piso_cents or 0, teto_cents or 10**12)
    else:
        band = price_band([x.price_cents for x in sinais])
    return [x for x in sinais if within_band(x.price_cents, band)]


def coletar_sinais(product_id: str, titulo: str,
                   teto_cents: int | None = None,
                   piso_cents: int | None = None) -> list[dict]:
    """Community offers for a product. They go to `deal_signals`, never to
    `price_points`: see the provider docstring for why."""
    novos: list[dict] = []
    for nome, prov in providers.feeds().items():
        try:
            sinais = (prov.sinais_do_titulo(titulo, 12)
                      if hasattr(prov, "sinais_do_titulo") else prov.sinais(titulo, 12))
        except Exception:                       # a broken feed must not stop the run
            continue
        for s in filtrar_por_preco(sinais, teto_cents, piso_cents):
            novo = db.upsert_signal(
                s.source, s.source_id, product_id, s.titulo, s.loja,
                s.price_cents, s.old_price_cents, s.desconto_pct, s.url,
                s.imagem, s.publicado_ts, getattr(s, "ativa", True))
            if novo and getattr(s, "ativa", True):
                novos.append({"loja": s.loja, "preco": brl(s.price_cents),
                              "titulo": s.titulo, "url": s.url,
                              "desconto": s.desconto_pct,
                              "plataforma": s.subcategoria})
    return novos


def _band_for(w: Any) -> tuple[int | None, int | None]:
    """Price band for a watched product. Around the CURRENT store price, not the
    historical minimum: a product that once had a deep sale has a low minimum,
    and a band built on it would drop today's real offers. With no store price
    at all (accessories), the watch's own target is the only plausible anchor."""
    pid = w["product_id"]
    atual = db.conn().execute(
        "SELECT MIN(pp.price_cents) m FROM price_points pp JOIN ("
        "  SELECT store, MAX(ts) ts FROM price_points WHERE product_id=?"
        "  GROUP BY store) u ON u.store=pp.store AND u.ts=pp.ts "
        "WHERE pp.product_id=?", (pid, pid)).fetchone()["m"]
    if atual:
        return int(atual * 1.25), int(atual * 0.35)
    if w["rule_kind"] == "price_below" and w["rule_value"]:
        teto = int(w["rule_value"] * 100)
        return teto, int(teto * 0.15)
    return None, None


def collect_feed(source: str = "promobit", *, now: int | None = None,
                 scheduled_for: int | None = None, verbose: bool = False) -> SourceRun:
    """Community feed, logged like any other source. Health for a feed is about
    whether the listing could be READ, not whether it matched our games: a
    front page with no game we track is a normal outcome."""
    prov = providers.feeds().get(source)
    run = SourceRun(source, run_id=jobs.start(source, scheduled_for, now=now))
    if prov is None:
        run.status, run.error, run.error_kind = jobs.SKIPPED, "feed not active", "other"
        jobs.finish(run.run_id, run.status, error=run.error, error_kind=run.error_kind, now=now)
        return run

    t0 = time.perf_counter()
    listing = prov.listing() if hasattr(prov, "listing") else []
    run.latency_ms = int((time.perf_counter() - t0) * 1000)
    run.items = len(listing)
    run.attempted = 1
    if not listing:
        run.error = getattr(prov, "last_error", "") or "feed returned nothing"
        run.error_kind = jobs.classify_error(run.error) or "drift"
        if run.error_kind in jobs.FAILURE_KINDS:
            run.failed = 1
    watches = db.active_watches()
    for w in watches:
        teto, piso = _band_for(w)
        for s in coletar_sinais(w["product_id"], w["title"], teto, piso):
            run.alerts.append({**s, "produto": w["title"]})
            alerts.notify(f"{w['title']} — oferta da comunidade",
                          f"{s['loja']}: {s['preco']}", s["url"])
            if verbose:
                print(f"  ~ COMUNIDADE {s['loja']}: {s['preco']} — {s['titulo'][:44]}")
    run.status = _status(run.attempted, run.failed)
    jobs.finish(run.run_id, run.status, items=run.items, attempted=run.attempted,
                failed=run.failed, latency_ms=run.latency_ms, error=run.error,
                error_kind=run.error_kind, now=now)
    return run


# --------------------------------------------------------------------- legacy

def run(verbose: bool = True) -> dict:
    """One-shot over everything: the CLI and the MCP `run_collection` tool."""
    store_runs = collect(verbose=verbose)
    feed_runs = [collect_feed(name, verbose=verbose) for name in providers.feeds()]
    fired = [a for r in store_runs.values() for a in r.alerts]
    signals = [a for r in feed_runs for a in r.alerts]
    watches = db.active_watches()
    return {"produtos": len({w["product_id"] for w in watches}),
            "watches": len(watches), "alertas": fired,
            "sinais_comunidade": signals,
            "fontes": {s: {"status": r.status, "itens": r.items,
                           "falhas": r.failed} for s, r in
                       {**store_runs, **{f.source: f for f in feed_runs}}.items()}}


def main() -> None:
    try:
        result = run(verbose=True)
        print(f"\nok: {result['watches']} watches, "
              f"{len(result['alertas'])} alertas novos")
    except Exception:
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
