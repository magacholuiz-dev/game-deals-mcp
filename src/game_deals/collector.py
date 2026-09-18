"""Coletor diario. Roda FORA do MCP (launchd/cron).

Este e o processo que da valor ao projeto: sem ele a serie temporal nunca cresce
e o veredito historico nunca sai do "sem historico ainda".
"""
from __future__ import annotations

import sys
import traceback

from . import alerts, db, providers
from .models import Offer, brl
from .verdict import evaluate as verdict_for


def refresh_product(product_id: str, quiet: bool = True) -> list[Offer]:
    """Consulta todas as fontes com alias para este produto e grava snapshot."""
    offers: list[Offer] = []
    for alias in db.aliases_for(product_id):
        prov = providers.get(alias["source"])
        if prov is None or not prov.configured():
            continue
        try:
            got = prov.fetch(alias["source_id"])
        except Exception as e:
            if not quiet:
                print(f"  ! {alias['source']}: {e}", file=sys.stderr)
            continue
        for o in got:
            db.record(product_id, o.source, o.store or o.source, o.price_cents,
                      o.regular_cents, o.currency, o.in_stock, o.url)
            db.set_image(product_id, o.image)   # so grava se ainda nao houver
        offers.extend(got)
    return offers


def coletar_sinais(product_id: str, titulo: str,
                   teto_cents: int | None = None,
                   piso_cents: int | None = None) -> list[dict]:
    """Ofertas da comunidade para um produto. Vai para `deal_signals`, nunca
    para `price_points` — ver o docstring do provider promobit."""
    novos: list[dict] = []
    for nome, prov in providers.feeds().items():
        try:
            sinais = (prov.sinais_do_titulo(titulo, 12)
                      if hasattr(prov, "sinais_do_titulo") else prov.sinais(titulo, 12))
            # Sem oferta ativa o produto ficaria sem preco nenhum. As encerradas
            # nao servem para comprar hoje, mas sao precos reais com data — o
            # unico historico gratuito que existe para o varejo brasileiro.
            if not sinais and hasattr(prov, "sinais_do_titulo"):
                sinais = prov.sinais_do_titulo(titulo, 12, incluir_encerradas=True)
        except Exception:
            continue

        # Sem preco de loja nao ha ancora — e ai entra tanto o console de R$ 6.999
        # na lista de um controle quanto o faceplate de R$ 15,83 na lista do GTA
        # VI. A propria distribuicao serve de ancora: a mediana das ofertas
        # casadas estima o produto, e o corte precisa ser SIMETRICO, porque o
        # ruido aparece dos dois lados — acessorio barato e bundle caro.
        if teto_cents is None and len(sinais) >= 3:
            precos = sorted(x.price_cents for x in sinais)
            mediana = precos[len(precos) // 2]
            sinais = [x for x in sinais
                      if mediana / 3 <= x.price_cents <= mediana * 3]

        for s in sinais:
            # Titulo nao separa acessorio de jogo: uma capinha de PS5 chamada
            # "Faceplate GTA VI" casa com a mesma busca do jogo. A banda de preco
            # corta o grosso (capinha de R$15 num jogo de R$350), e o card mostra
            # o titulo da oferta para voce julgar o que sobrar.
            if teto_cents and s.price_cents > teto_cents:
                continue
            if piso_cents and s.price_cents < piso_cents:
                continue
            novo = db.upsert_signal(
                s.source, s.source_id, product_id, s.titulo, s.loja,
                s.price_cents, s.old_price_cents, s.desconto_pct, s.url,
                s.imagem, s.publicado_ts, getattr(s, "ativa", True))
            # Oferta encerrada e historico, nao noticia: grava mas nao alerta.
            if novo and getattr(s, "ativa", True):
                novos.append({"loja": s.loja, "preco": brl(s.price_cents),
                              "titulo": s.titulo, "url": s.url,
                              "desconto": s.desconto_pct,
                              "plataforma": s.subcategoria})
    return novos


def run(verbose: bool = True) -> dict:
    watches = db.active_watches()
    fired: list[dict] = []
    seen_products: set[str] = set()

    for w in watches:
        pid = w["product_id"]
        # Snapshot da leitura anterior ANTES de gravar a nova — as regras
        # any_drop e back_in_stock dependem dessa comparacao.
        prev_by_store: dict[str, dict] = {}
        for row in db.conn().execute(
                "SELECT store, price_cents, in_stock, MAX(ts) AS ts FROM price_points "
                "WHERE product_id=? GROUP BY store", (pid,)).fetchall():
            prev_by_store[row["store"]] = dict(row)

        offers = refresh_product(pid, quiet=not verbose)
        seen_products.add(pid)
        if verbose:
            print(f"- {w['title']}: {len(offers)} ofertas")

        for o in offers:
            store = o.store or o.source
            v = verdict_for(pid, store, o.price_cents)
            headline = alerts.evaluate(w["rule_kind"], w["rule_value"], o, v,
                                       prev_by_store.get(store))
            if not headline:
                continue
            if db.alert_exists_recently(pid, store, o.price_cents):
                continue   # a promo ja foi avisada; nao repete todo dia
            msg = f"{store}: {headline} ({v.label})"
            db.add_alert(w["id"], pid, o.source, store, o.price_cents, msg, o.url)
            alerts.notify(w["title"], msg, o.url)
            fired.append({"produto": w["title"], "loja": store,
                          "preco": brl(o.price_cents), "motivo": msg,
                          "url": o.url})
            if verbose:
                print(f"  * ALERTA {msg}")

    # Sinais da comunidade, depois dos precos: o teto usa o melhor preco de loja
    # conhecido, entao precisa das leituras deste ciclo ja gravadas.
    sinais_novos = []
    for w in watches:
        pid = w["product_id"]
        # Teto sobre o preco ATUAL, nao sobre o minimo historico: se o produto ja
        # esteve em promocao forte um dia, o minimo historico e baixo demais e o
        # filtro derrubaria justamente as ofertas reais de hoje.
        atual = db.conn().execute(
            "SELECT MIN(pp.price_cents) m FROM price_points pp JOIN ("
            "  SELECT store, MAX(ts) ts FROM price_points WHERE product_id=?"
            "  GROUP BY store) u ON u.store=pp.store AND u.ts=pp.ts "
            "WHERE pp.product_id=?", (pid, pid)).fetchone()["m"]
        # Produto sem loja com API (acessorio, edicao especial) nao tem preco de
        # referencia — e ai qualquer coisa que case com a busca entra, inclusive
        # um console de R$ 6.999 na lista de um controle. Nesse caso o alvo do
        # proprio watch serve de teto: `watch(..., "price_below", 500)` diz ao
        # coletor o que e plausivel para aquele produto.
        if atual:
            teto = int(atual * 1.25)
            piso = int(atual * 0.35)
        elif w["rule_kind"] == "price_below" and w["rule_value"]:
            teto = int(w["rule_value"] * 100)
            piso = int(teto * 0.15)
        else:
            teto = piso = None
        for s in coletar_sinais(pid, w["title"], teto, piso):
            sinais_novos.append({**s, "produto": w["title"]})
            if verbose:
                print(f"  ~ COMUNIDADE {s['loja']}: {s['preco']} — {s['titulo'][:44]}")
            alerts.notify(f"{w['title']} — oferta da comunidade",
                          f"{s['loja']}: {s['preco']}", s["url"])

    return {"produtos": len(seen_products), "watches": len(watches),
            "alertas": fired, "sinais_comunidade": sinais_novos}


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
