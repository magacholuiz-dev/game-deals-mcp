"""Dashboard local.

Serve o SQLite para o navegador. O selo e o veredito NAO sao recalculados aqui —
vem prontos de verdict.py, os mesmos que o MCP e o coletor usam. O front so
desenha.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from . import collector, db
from .models import brl
from .providers.nintendo import ROTULO as ROTULO_COMPAT
from .verdict import evaluate as verdict_for, highlight

app = FastAPI(title="game-deals")
STATIC = Path(__file__).parent / "static"

DAY = 86400


def _sparkline(product_id: str, days: int = 365) -> list[dict]:
    """Minimo por dia entre todas as lojas — a linha que interessa e 'qual era o
    melhor preco disponivel naquele dia', nao o preco de uma loja especifica."""
    rows = db.conn().execute(
        "SELECT CAST(ts/86400 AS INT) AS dia, MIN(price_cents) AS p "
        "FROM price_points WHERE product_id=? AND ts>=? "
        "GROUP BY dia ORDER BY dia",
        (product_id, db.now() - days * DAY)).fetchall()
    return [{"data": dt.datetime.fromtimestamp(r["dia"] * DAY).strftime("%Y-%m-%d"),
             "preco_cents": r["p"]} for r in rows]


def _card(p) -> dict:
    pid = p["id"]
    # Ultima leitura de cada loja: o card mostra o estado atual sem ir a rede.
    rows = db.conn().execute(
        "SELECT pp.* FROM price_points pp JOIN ("
        "  SELECT store, MAX(ts) AS ts FROM price_points WHERE product_id=?"
        "  GROUP BY store) m ON m.store=pp.store AND m.ts=pp.ts "
        "WHERE pp.product_id=? ORDER BY pp.price_cents",
        (pid, pid)).fetchall()

    ofertas = []
    for r in rows:
        v = verdict_for(pid, r["store"], r["price_cents"])
        h = highlight(v)
        ofertas.append({
            "loja": r["store"], "preco": brl(r["price_cents"]),
            "preco_cents": r["price_cents"],
            "de": brl(r["regular_cents"]) if r["regular_cents"] else None,
            "desconto_pct": (round(100 * (1 - r["price_cents"] / r["regular_cents"]))
                             if r["regular_cents"] and r["regular_cents"] > 0 else 0),
            "em_estoque": bool(r["in_stock"]), "url": r["url"],
            "veredito": v.label, "confianca": v.confidence,
            "selo": h,
            "minimo_90d": brl(v.min_90d_cents),
            "minimo_historico": brl(v.min_all_cents),
            "atualizado": dt.datetime.fromtimestamp(r["ts"]).strftime("%d/%m %H:%M"),
        })

    melhor = ofertas[0] if ofertas else None
    watches = [{"id": w["id"], "regra": w["rule_kind"], "valor": w["rule_value"]}
               for w in db.active_watches() if w["product_id"] == pid]

    return {
        "product_id": pid, "titulo": p["title"], "categoria": p["category"],
        "plataforma": p["platform"], "imagem": p["image_url"],
        "compat": p["compat"],
        "compat_rotulo": ROTULO_COMPAT.get(p["compat"], ""),
        "nota": p["metacritic"], "nota_usuarios": p["user_rating"],
        "avaliacoes": p["ratings_count"], "popularidade": p["popularity"],
        "relevancia": p["relevance"], "lancamento": p["released"],
        "melhor": melhor, "ofertas": ofertas,
        "selo": melhor["selo"] if melhor else None,
        "historico": _sparkline(pid),
        "comunidade": [
            {"loja": r["store"], "preco": brl(r["price_cents"]),
             "preco_cents": r["price_cents"], "titulo": r["title"],
             "desconto_pct": round(r["discount_pct"]), "url": r["url"]}
            for r in db.signals_for(pid, 4)],
        "ja_esteve": [
            {"loja": r["store"], "preco": brl(r["price_cents"]),
             "titulo": r["title"], "url": r["url"],
             "quando": (dt.datetime.fromtimestamp(r["published_ts"])
                        .strftime("%d/%m/%y") if r["published_ts"] else "")}
            for r in db.conn().execute(
                "SELECT * FROM deal_signals WHERE product_id=? AND active=0 "
                "ORDER BY price_cents LIMIT 3", (pid,)).fetchall()],
        "watches": watches,
        "leituras": db.conn().execute(
            "SELECT COUNT(*) c FROM price_points WHERE product_id=?",
            (pid,)).fetchone()["c"],
    }


ORDEM_SELO = {"historico": 0, "destaque": 1, "bom": 2}


@app.get("/api/cards")
def cards(sort: str = "relevancia") -> JSONResponse:
    """sort=relevancia (nota x popularidade) ou sort=destaques (oportunidade)."""
    data = [_card(p) for p in db.list_products()]

    if sort == "destaques":
        data.sort(key=lambda c: (ORDEM_SELO.get((c["selo"] or {}).get("tier"), 9),
                                 -(c["melhor"]["desconto_pct"] if c["melhor"] else 0)))
    else:
        # Sem nota vai para o fim, nao para o topo com relevancia 0.
        data.sort(key=lambda c: (c["relevancia"] is None,
                                 -(c["relevancia"] or 0),
                                 ORDEM_SELO.get((c["selo"] or {}).get("tier"), 9)))

    return JSONResponse({"produtos": data, "ordem": sort,
                         "gerado_em": dt.datetime.now().strftime("%d/%m/%Y %H:%M")})


@app.post("/api/refresh/{product_id}")
def refresh_one(product_id: str) -> JSONResponse:
    offers = collector.refresh_product(product_id, quiet=True)
    return JSONResponse({"ok": True, "ofertas": len(offers)})


@app.post("/api/refresh")
def refresh_all() -> JSONResponse:
    n = 0
    for p in db.list_products():
        n += len(collector.refresh_product(p["id"], quiet=True))
    return JSONResponse({"ok": True, "ofertas": n})


@app.get("/api/alerts")
def alerts() -> JSONResponse:
    return JSONResponse({"alertas": [
        {"id": r["id"], "produto": r["title"], "loja": r["store"],
         "preco": brl(r["price_cents"]), "motivo": r["headline"], "url": r["url"],
         "quando": dt.datetime.fromtimestamp(r["ts"]).strftime("%d/%m %H:%M")}
        for r in db.open_alerts()]})


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


def main() -> None:
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8787, log_level="warning")


if __name__ == "__main__":
    main()
