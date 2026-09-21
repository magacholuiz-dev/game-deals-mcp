"""Wishlist: você diz o nome, o sistema resolve nota, capa, preço e vigia.

Três formas de pedir, porque elas falham de jeitos diferentes:

- **título exato** ("Ghost of Yotei", "Marvel's Wolverine") — casamento preciso
  do RAWG. Funciona até para jogo não lançado, que ainda não tem nota.
- **franquia** ("Zelda", "Mario") — busca ampla dentro de uma plataforma. Sem o
  recorte de plataforma o RAWG devolve um indie chamado ZELDA na frente do jogo
  da Nintendo: casamento exato de string ganha da relevância.
- **clássicos que você perdeu** — os melhores do Switch 1, que rodam no Switch 2
  por retrocompatibilidade.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from . import db, providers, ratings
from .matching import same_game
from .collector import coletar_sinais, refresh_product
from .models import brl
from .providers.nintendo import NATIVO, RETRO


@dataclass
class Resultado:
    product_id: str
    titulo: str
    plataforma: str
    metacritic: int | None
    relevancia: float
    preco_cents: int | None
    origem: str          # eshop | varejo | sem preço
    compat: str
    ofertas_comunidade: int


def slug(titulo: str) -> str:
    t = unicodedata.normalize("NFKD", titulo).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")[:44]


def _bate(pedido: str, achado: str) -> bool:
    """O Solr da Nintendo casa por relevância, não por igualdade: buscar
    "Mario Kart 8 Deluxe" devolve "Mario Kart World" como primeiro resultado.
    Aceitar cegamente colava o preço de um jogo no card de outro — R$ 439,90 do
    World virava o preço do 8 Deluxe. Exigimos os tokens distintivos."""
    return same_game(pedido, achado)


def _pid_for(titulo: str, plataforma: str) -> str:
    """Id do produto. O mesmo título em plataformas diferentes NÃO é o mesmo
    produto: Zelda BotW no Switch 1 (R$ 329,90) e a Switch 2 Edition (R$ 389,90)
    dividiam um id e um alias por fonte, e a série de preços misturava os dois."""
    pid = slug(titulo)
    ex = db.get_product(pid)
    if ex and plataforma and ex["platform"] and ex["platform"] != plataforma:
        return f"{pid}-{plataforma}"
    return pid


def _existe_no_switch2(titulo: str) -> bool:
    """O RAWG lista o jogo como "Switch", que não diz se há versão de Switch 2.
    Quem diz é o catálogo da Nintendo, filtrado por Switch 2."""
    nin = providers.get("nintendo")
    return any(_bate(titulo, a.title)
               for a in nin.search(titulo, 5, apenas_switch2=True))


def _preco(pid: str, titulo: str, plataforma: str) -> tuple[int | None, str, int]:
    """Tenta eShop (só Nintendo), depois varejo brasileiro. Devolve
    (preço, origem, nº de ofertas da comunidade)."""
    if plataforma in ("switch", "switch2"):
        nin = providers.get("nintendo")
        achados = nin.search(titulo, 5, apenas_switch2=(plataforma == "switch2"))
        casou = next((a for a in achados if _bate(titulo, a.title)), None)
        if casou:
            db.replace_alias(pid, "nintendo", casou.source_id)
            ofertas = refresh_product(pid, quiet=True)
            if ofertas:
                return min(o.price_cents for o in ofertas), "eshop", 0
            db.conn().execute("DELETE FROM aliases WHERE product_id=? AND source=?",
                              (pid, "nintendo"))
            db.conn().commit()

    sinais = coletar_sinais(pid, titulo)
    if not sinais:
        sinais = coletar_sinais(pid, titulo)  # já cai para encerradas lá dentro
    linha = db.cheapest_signal(pid)
    if linha:
        return linha["price_cents"], "varejo", len(db.signals_for(pid, 9))
    return None, "sem preço", 0


def adiciona(titulo_pedido: str, plataforma: str = "", regra: str = "new_low",
             preciso: bool = True, ficha: ratings.Ficha | None = None
             ) -> Resultado | None:
    """Resolve um nome e coloca na wishlist."""
    if ficha is None:
        if not ratings.configured():
            return None
        achadas = ratings.buscar(titulo_pedido, 5, plataforma, preciso)
        if not achadas:
            return None
        ficha = achadas[0]

    if plataforma == "switch2" and not _existe_no_switch2(ficha.titulo):
        return None                    # não há versão de Switch 2: não vira produto
    pid = _pid_for(ficha.titulo, plataforma)
    compat = NATIVO if plataforma == "switch2" else (
        RETRO if plataforma == "switch" else "")
    db.upsert_product(pid, ficha.titulo, "game", plataforma or "", ficha.imagem,
                      compat)
    ratings.salvar(pid, ficha)

    preco, origem, n_com = _preco(pid, ficha.titulo, plataforma)

    if not any(w["product_id"] == pid for w in db.active_watches()):
        # Jogo não lançado não entra em promoção: a regra útil é qualquer queda.
        nao_lancado = ficha.metacritic is None and bool(ficha.lancamento)
        db.add_watch(pid, "any_drop" if nao_lancado else regra, None, "wishlist")

    return Resultado(pid, ficha.titulo, plataforma, ficha.metacritic,
                     ficha.relevancia, preco, origem, compat, n_com)


def franquia(termo: str, plataforma: str = "switch", quantos: int = 5,
             min_metacritic: int = 0) -> list[Resultado]:
    """Os melhores jogos de uma franquia numa plataforma."""
    if not ratings.configured():
        return []
    achadas = ratings.buscar(termo, quantos * 3, plataforma, preciso=False)
    out = []
    for f in achadas:
        if len(out) >= quantos:
            break
        if min_metacritic and (f.metacritic or 0) < min_metacritic:
            continue
        r = adiciona(f.titulo, plataforma, ficha=f)
        if r:
            out.append(r)
    return out


def classicos_switch1(quantos: int = 15, min_metacritic: int = 85,
                      desde: str = "2017-03-03") -> list[Resultado]:
    """Os melhores do Switch 1 — que rodam no Switch 2 por retrocompatibilidade.

    `desde` corta em 03/03/2017, lançamento do Switch: sem isso o RAWG devolve
    Ocarina of Time e A Link to the Past, que estão no catálogo por emulação do
    Switch Online e não são o que "perdi no Switch" quer dizer.
    """
    if not ratings.configured():
        return []
    fichas = ratings.top("switch", min_metacritic, quantos * 3, desde)
    out = []
    for f in fichas:
        if len(out) >= quantos:
            break
        r = adiciona(f.titulo, "switch", ficha=f)
        if r:
            out.append(r)
    return out


def formata(r: Resultado) -> str:
    mc = f"{r.metacritic:>3}" if r.metacritic else " --"
    preco = brl(r.preco_cents) if r.preco_cents else "—"
    extra = f" ({r.ofertas_comunidade} ofertas)" if r.ofertas_comunidade else ""
    return (f"  [{mc}] relev {r.relevancia:>5}  {preco:>11}  {r.origem:<10} "
            f"{r.titulo[:38]:40}{extra}")
