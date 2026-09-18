"""Servidor MCP.

As tools sao finas de proposito: elas leem o core. Toda a inteligencia
(veredito historico, regras de alerta) mora em verdict.py e alerts.py, para
que o coletor diario use exatamente a mesma logica sem passar pelo MCP.
"""
from __future__ import annotations

import uuid
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import alerts as alerts_mod
from . import collector, db, providers, ratings
from .models import brl
from .verdict import evaluate as verdict_for

mcp = MCPServer(
    "game-deals",
    version="0.1.0",
    instructions=(
        "Rastreia preco de jogos e hardware no Brasil. O veredito historico "
        "('menor preco dos ultimos N dias') ja vem calculado em get_price — "
        "use-o como esta, nao tente reinferir da lista de precos. Precos em "
        "BRL. Fontes sem credencial ficam inertes: veja sources_status()."
    ),
)


def _slug(title: str) -> str:
    base = "".join(ch.lower() if ch.isalnum() else "-" for ch in title).strip("-")
    while "--" in base:
        base = base.replace("--", "-")
    return (base[:40] or "produto") + "-" + uuid.uuid4().hex[:4]


# ---------------------------------------------------------------- descoberta

@mcp.tool()
def sources_status() -> list[dict]:
    """Quais fontes de preco estao ativas e o que falta configurar nas outras."""
    return providers.status()


@mcp.tool()
def search_sources(query: str, sources: list[str] | None = None,
                   limit: int = 6) -> dict:
    """Busca um jogo ou acessorio nas fontes configuradas.

    Devolve candidatos com o `source_id` de cada fonte — use-os em
    `register_product` para criar o produto rastreado.
    """
    targets = providers.available()
    if sources:
        targets = {n: p for n, p in targets.items() if n in sources}

    results: dict[str, Any] = {}
    for name, prov in targets.items():
        try:
            results[name] = [x.dict() for x in prov.search(query, limit)]
        except Exception as e:
            results[name] = {"erro": str(e)[:200]}

    inativas = [s["fonte"] for s in providers.status() if not s["configurado"]]
    return {"query": query, "resultados": results,
            "fontes_inativas": inativas}


# ---------------------------------------------------------------- catalogo

@mcp.tool()
def register_product(title: str, aliases: list[dict], category: str = "game",
                     platform: str = "", product_id: str = "") -> dict:
    """Cria um produto rastreado ligando os ids das varias fontes.

    `aliases`: [{"source": "mercadolivre", "source_id": "MLB1234", "url": "..."}]

    A identidade de produto e o problema dificil aqui, nao a coleta: "GTA VI PS5"
    na Amazon, no ML e na Shopee sao tres strings diferentes. Ligue a mao — casar
    por similaridade de titulo gera alerta no produto errado.
    """
    pid = product_id or _slug(title)
    db.upsert_product(pid, title, category, platform)
    for a in aliases:
        src = a.get("source", "")
        if src not in providers.ALL:
            return {"erro": f"fonte desconhecida: {src}",
                    "fontes_validas": list(providers.ALL)}
        db.add_alias(pid, src, str(a.get("source_id", "")),
                     a.get("label", ""), a.get("url", ""))
    return {"product_id": pid, "titulo": title,
            "aliases": [dict(r) for r in db.aliases_for(pid)]}


@mcp.tool()
def list_tracked() -> list[dict]:
    """Todos os produtos no catalogo, com quantas fontes e leituras cada um tem."""
    out = []
    for p in db.list_products():
        n = db.conn().execute(
            "SELECT COUNT(*) c FROM price_points WHERE product_id=?",
            (p["id"],)).fetchone()["c"]
        out.append({"product_id": p["id"], "titulo": p["title"],
                    "categoria": p["category"], "plataforma": p["platform"],
                    "fontes": len(db.aliases_for(p["id"])), "leituras": n})
    return out


# ---------------------------------------------------------------- precos

@mcp.tool()
def get_price(product: str) -> dict:
    """Preco atual em todas as fontes ligadas, ordenado do mais barato,
    com o veredito historico de cada loja ("menor preco dos ultimos N dias").

    Aceita product_id ou parte do titulo. Grava a leitura no historico.
    """
    pid = db.resolve(product)
    if not pid:
        return {"erro": f"produto nao encontrado: {product}",
                "dica": "use register_product primeiro, ou list_tracked para ver os ids"}

    offers = collector.refresh_product(pid, quiet=True)
    if not offers:
        return {"product_id": pid, "ofertas": [],
                "aviso": "nenhuma fonte configurada respondeu — veja sources_status()"}

    rows = []
    for o in offers:
        store = o.store or o.source
        v = verdict_for(pid, store, o.price_cents)
        rows.append({
            "loja": store, "fonte": o.source,
            "preco": brl(o.price_cents), "preco_cents": o.price_cents,
            "de": brl(o.regular_cents) if o.regular_cents else None,
            "desconto_pct": o.cut_pct,
            "em_estoque": o.in_stock,
            "veredito": v.label,
            "confianca": v.confidence,
            "minimo_90d": brl(v.min_90d_cents),
            "minimo_historico": brl(v.min_all_cents),
            "url": o.url,
            "obs": v.caveat or None,
            **({"promo_ate": o.extra["promo_ate"]} if o.extra.get("promo_ate") else {}),
        })
    rows.sort(key=lambda r: r["preco_cents"])
    p = db.get_product(pid)
    return {"product_id": pid, "titulo": p["title"] if p else pid,
            "melhor": rows[0], "ofertas": rows}


@mcp.tool()
def price_history(product: str, store: str = "", days: int = 365) -> dict:
    """Serie temporal de precos, para comentar tendencia e sazonalidade."""
    pid = db.resolve(product)
    if not pid:
        return {"erro": f"produto nao encontrado: {product}"}
    since = db.now() - days * 86400
    pts = db.history(pid, store or None, since)
    return {
        "product_id": pid, "loja": store or "todas", "dias": days,
        "pontos": [{"data": __import__("datetime").datetime.fromtimestamp(p["ts"])
                    .strftime("%Y-%m-%d"), "loja": p["store"],
                    "preco": brl(p["price_cents"]),
                    "preco_cents": p["price_cents"]} for p in pts],
        "total": len(pts),
    }


@mcp.tool()
def seed_history_from_itad(product: str) -> dict:
    """Semeia o historico de um jogo de PC com o log do IsThereAnyDeal.

    Sem isso voce comeca cego e o veredito so fica util depois de ~2 meses
    coletando. Com isso, o produto ja nasce com anos de historico no dia 1.
    """
    pid = db.resolve(product)
    if not pid:
        return {"erro": f"produto nao encontrado: {product}"}
    alias = next((a for a in db.aliases_for(pid) if a["source"] == "itad"), None)
    if not alias:
        return {"erro": "este produto nao tem alias 'itad'",
                "dica": "so funciona para jogos de PC; ligue o alias com register_product"}

    log = providers.itad.provider.seed_history(alias["source_id"])
    rows = []
    for entry in log:
        deal = entry.get("deal") or {}
        price = (deal.get("price") or {})
        amt = price.get("amountInt")
        ts = entry.get("timestamp")
        if amt is None or not ts:
            continue
        import datetime as _dt
        try:
            epoch = int(_dt.datetime.fromisoformat(
                str(ts).replace("Z", "+00:00")).timestamp())
        except ValueError:
            continue
        shop = (deal.get("shop") or {}).get("name", "?")
        regular = (deal.get("regular") or {}).get("amountInt")
        rows.append((pid, "itad", shop, int(amt), regular,
                     price.get("currency", "BRL"), 1, "", epoch))
    n = db.record_many(rows) if rows else 0
    return {"product_id": pid, "pontos_importados": n,
            "aviso": "" if n else "ITAD nao devolveu historico (chave ausente ou jogo novo)"}


# ---------------------------------------------------------------- notas

@mcp.tool()
def discover_top(platform: str = "switch", min_metacritic: int = 80,
                 limit: int = 15, since: str = "",
                 register: bool = False) -> dict:
    """Os maiores jogos de uma plataforma: nota da crítica alta E muita gente jogando.

    `platform`: switch | ps5 | ps4 | pc | xbox
    `since`: opcional, "AAAA-MM-DD" para limitar a lançamentos recentes.
    `register`: cria os produtos no catálogo (sem preço ainda — ligue os aliases
    depois com `link_store`).

    Ordena por `relevancia` = 60% nota + 40% popularidade em escala log.

    Atenção ao que "popularidade" é: número de pessoas que adicionaram o jogo à
    biblioteca no RAWG. Vendas por título não são públicas em API nenhuma; este é
    o proxy honesto, e correlaciona bem para jogos grandes.
    """
    if not ratings.configured():
        return {"erro": "defina RAWG_API_KEY no .env",
                "como": "chave gratuita (só e-mail) em https://rawg.io/apidocs"}

    fichas = ratings.top(platform, min_metacritic, limit, since)
    if not fichas:
        return {"erro": f"nada encontrado para {platform}",
                "plataformas": list(ratings.PLATAFORMAS)}

    criados = []
    if register:
        for f in fichas:
            pid = _slug(f.titulo)
            existente = db.resolve(f.titulo)
            pid = existente or pid
            db.upsert_product(pid, f.titulo, "game", platform, f.imagem)
            db.set_ratings(pid, f.rawg_id, f.metacritic, f.nota_usuarios,
                           f.avaliacoes, f.popularidade, f.relevancia,
                           f.lancamento)
            criados.append(pid)

    return {"plataforma": platform, "total": len(fichas),
            "registrados": criados,
            "jogos": [{**f.dict(), "posicao": i + 1}
                      for i, f in enumerate(fichas)]}


@mcp.tool()
def enrich_ratings(product: str, titulo_busca: str = "") -> dict:
    """Anexa nota da crítica e popularidade a um produto já rastreado."""
    if not ratings.configured():
        return {"erro": "defina RAWG_API_KEY no .env"}
    pid = db.resolve(product)
    if not pid:
        return {"erro": f"produto não encontrado: {product}"}
    p = db.get_product(pid)
    fichas = ratings.buscar(titulo_busca or p["title"], 5)
    if not fichas:
        return {"erro": "RAWG não achou nada com esse título",
                "dica": "passe titulo_busca com o nome em inglês"}
    f = fichas[0]
    db.set_ratings(pid, f.rawg_id, f.metacritic, f.nota_usuarios, f.avaliacoes,
                   f.popularidade, f.relevancia, f.lancamento)
    if not p["image_url"]:
        db.set_image(pid, f.imagem)
    return {"product_id": pid, "casou_com": f.titulo, "ficha": f.dict(),
            "confira": "o casamento é pelo 1º resultado da busca — verifique o título"}


@mcp.tool()
def link_store(product: str, source: str, source_id: str) -> dict:
    """Liga um produto já no catálogo a uma loja, para começar a ter preço.

    PlayStation: o id vem da URL da loja — store.playstation.com/pt-br/concept/10000493
    → source_id "10000493". A busca da PS Store é uma SPA e não dá para ler.
    """
    pid = db.resolve(product)
    if not pid:
        return {"erro": f"produto não encontrado: {product}"}
    if source not in providers.ALL:
        return {"erro": f"fonte desconhecida: {source}",
                "fontes": list(providers.ALL)}
    db.add_alias(pid, source, source_id)
    offers = collector.refresh_product(pid, quiet=True)
    return {"product_id": pid, "fonte": source, "ofertas": len(offers),
            "precos": [{"loja": o.store, "preco": brl(o.price_cents)} for o in offers]}


@mcp.tool()
def community_deals(query: str = "", product: str = "", limit: int = 12) -> dict:
    """Ofertas postadas no Promobit — cobre KaBuM!, Netshoes, Magazine Luiza,
    Casas Bahia, Amazon e Shopee de uma vez.

    Passe `query` para busca livre, ou `product` para um produto já rastreado.

    Estes preços NÃO entram no histórico: são posts pontuais de usuários, podem
    estar errados ou expirados, e misturá-los com as leituras periódicas das
    lojas estragaria o veredito "menor preço em X tempo". Trate como pista.
    """
    feeds = providers.feeds()
    if not feeds:
        return {"erro": "nenhuma fonte de comunidade ativa"}

    termo = query
    pid = None
    if product:
        pid = db.resolve(product)
        if not pid:
            return {"erro": f"produto não encontrado: {product}"}
        termo = termo or db.get_product(pid)["title"]
    if not termo:
        return {"erro": "informe `query` ou `product`"}

    out = []
    for nome, prov in feeds.items():
        for s in prov.sinais(termo, limit):
            out.append({"fonte": nome, **s.dict()})
    out.sort(key=lambda x: x["price_cents"])

    return {"busca": termo, "product_id": pid, "total": len(out),
            "ofertas": out[:limit],
            "aviso": "preços postados por usuários — confira antes de comprar"}


# ---------------------------------------------------------------- watchlist

@mcp.tool()
def watch(product: str, rule: str = "new_low", value: float | None = None,
          note: str = "") -> dict:
    """Coloca um produto na watchlist.

    Regras:
      price_below     — dispara abaixo de R$ `value`
      discount_above  — dispara com desconto >= `value` %
      new_low         — dispara no menor preco ja registrado
      any_drop        — qualquer queda vs. a leitura anterior
      back_in_stock   — voltou ao estoque (use para pre-venda e edicao especial,
                        que nao entram em desconto mas esgotam)
    """
    pid = db.resolve(product)
    if not pid:
        return {"erro": f"produto nao encontrado: {product}"}
    if rule not in alerts_mod.RULES:
        return {"erro": f"regra invalida: {rule}", "regras": alerts_mod.RULES}
    if rule in ("price_below", "discount_above") and value is None:
        return {"erro": f"a regra {rule} exige `value`"}
    wid = db.add_watch(pid, rule, value, note)
    return {"watch_id": wid, "product_id": pid, "regra": rule, "valor": value}


@mcp.tool()
def list_watches() -> list[dict]:
    """Watchlist ativa."""
    return [{"watch_id": w["id"], "produto": w["title"],
             "product_id": w["product_id"], "regra": w["rule_kind"],
             "valor": w["rule_value"], "nota": w["note"]}
            for w in db.active_watches()]


@mcp.tool()
def unwatch(watch_id: int) -> dict:
    """Desativa um item da watchlist."""
    db.set_watch_active(watch_id, False)
    return {"watch_id": watch_id, "ativo": False}


# ---------------------------------------------------------------- alertas

@mcp.tool()
def pending_alerts(acknowledge: bool = False) -> dict:
    """Alertas que dispararam desde a ultima checagem.

    O coletor diario ja envia push; esta tool e para revisar dentro da conversa.
    """
    rows = db.open_alerts()
    out = [{"id": r["id"], "produto": r["title"], "loja": r["store"],
            "preco": brl(r["price_cents"]), "motivo": r["headline"],
            "url": r["url"],
            "quando": __import__("datetime").datetime.fromtimestamp(r["ts"])
            .strftime("%d/%m %H:%M")} for r in rows]
    if acknowledge and out:
        db.ack_alerts([r["id"] for r in rows])
    return {"total": len(out), "alertas": out,
            "marcados_como_lidos": bool(acknowledge and out)}


@mcp.tool()
def run_collection() -> dict:
    """Roda a coleta de toda a watchlist agora (o mesmo que o cron diario faz)."""
    return collector.run(verbose=False)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
