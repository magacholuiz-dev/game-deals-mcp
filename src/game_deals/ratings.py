"""Notas da crítica e popularidade, via RAWG.

**Sobre "muita venda":** número de vendas não é público em lugar nenhum — nem a
Nintendo nem a Sony divulgam por título fora dos relatórios trimestrais, e não
existe API disso. O que dá para usar é *proxy de popularidade*: quantas pessoas
adicionaram o jogo à biblioteca (`added`) e quantas avaliaram (`ratings_count`).
Correlaciona bem com vendas para jogos grandes, mas não é venda — os campos aqui
se chamam `popularidade`, não `vendas`, de propósito.

RAWG: chave gratuita em https://rawg.io/apidocs (só e-mail).
Traz `metacritic` direto, além de nota de usuário, contagens, data e capa.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, asdict
from typing import Any

from .providers.base import client

API = "https://api.rawg.io/api"

# Plataformas RAWG (ids estáveis, conferidos em /platforms).
#
# O RAWG **não tem Nintendo Switch 2** — só "Nintendo Switch" (id 7). Por isso
# "switch2" também aponta para 7: a nota e a popularidade de um jogo são as
# mesmas nas duas versões (um "Switch 2 Edition" é o mesmo jogo). Quem diz se o
# título existe no Switch 2 é o catálogo da Nintendo, cruzado depois — ver
# `scripts/setup_watchlist.py`. RAWG ranqueia; Nintendo confirma a plataforma.
PLATAFORMAS = {
    "switch": 7, "switch2": 7,
    "ps5": 187, "ps4": 18,
    "pc": 4, "xbox": 1,
}

# `added` de um blockbuster fica na casa dos 10-20 mil. log10(10000)=4.
_POP_TETO_LOG = 4.0


def api_key() -> str:
    return (os.environ.get("RAWG_API_KEY") or "").strip()


def configured() -> bool:
    return bool(api_key())


@dataclass
class Ficha:
    rawg_id: int
    titulo: str
    metacritic: int | None
    nota_usuarios: float | None      # 0-5
    avaliacoes: int
    popularidade: int                # `added` — proxy, não vendas
    lancamento: str
    imagem: str
    plataformas: list[str]
    relevancia: float                # 0-100, combinação nota × popularidade
    playtime_horas: float | None = None   # RAWG average hours; 0 means unknown
    publishers: list[str] | None = None   # only present in the detail payload

    def dict(self) -> dict[str, Any]:
        return asdict(self)


def relevancia(metacritic: int | None, nota_usuarios: float | None,
               popularidade: int) -> float:
    """Nota (60%) × popularidade em escala log (40%).

    A escala log importa: sem ela, um único blockbuster com 19 mil adds achata
    todo o resto para perto de zero e o ranking vira "o mais popular vence",
    ignorando a nota — que é justamente o que se quer evitar.
    """
    if metacritic:
        nota = metacritic / 100
    elif nota_usuarios:
        nota = nota_usuarios / 5
    else:
        nota = 0.0

    pop = min(1.0, math.log10(1 + max(0, popularidade)) / _POP_TETO_LOG)
    return round(100 * (0.6 * nota + 0.4 * pop), 1)


def _ficha(g: dict) -> Ficha:
    pop = int(g.get("added") or 0)
    mc = g.get("metacritic")
    nu = g.get("rating")
    return Ficha(
        rawg_id=int(g.get("id", 0)),
        titulo=g.get("name", ""),
        metacritic=int(mc) if mc else None,
        nota_usuarios=float(nu) if nu else None,
        avaliacoes=int(g.get("ratings_count") or 0),
        popularidade=pop,
        lancamento=g.get("released") or "",
        imagem=g.get("background_image") or "",
        plataformas=[(p.get("platform") or {}).get("name", "")
                     for p in (g.get("parent_platforms") or g.get("platforms") or [])],
        relevancia=relevancia(int(mc) if mc else None,
                              float(nu) if nu else None, pop),
        playtime_horas=float(g["playtime"]) if g.get("playtime") else None,
        publishers=[p.get("name", "") for p in (g.get("publishers") or [])] or None,
    )


def _get(path: str, **params) -> dict:
    if not configured():
        return {}
    params["key"] = api_key()
    with client() as c:
        r = c.get(f"{API}/{path}", params=params)
        if r.status_code >= 400:
            return {"_error": r.status_code, "_body": r.text[:200]}
        return r.json() or {}


def salvar(product_id: str, f: Ficha) -> None:
    """Store everything a RAWG record gives us, in one place."""
    from . import db
    db.set_ratings(product_id, f.rawg_id, f.metacritic, f.nota_usuarios,
                   f.avaliacoes, f.popularidade, f.relevancia, f.lancamento)
    db.set_playtime(product_id, f.playtime_horas)
    if f.publishers:
        db.set_publisher(product_id, f.publishers[0])


def refresh(rawg_id: int) -> Ficha | None:
    """Current numbers for a game we already know by its RAWG id."""
    d = _get(f"games/{int(rawg_id)}")
    if not d or d.get("_error") or not d.get("id"):
        return None
    return _ficha(d)


def buscar(titulo: str, limite: int = 5, plataforma: str = "",
           preciso: bool = True) -> list[Ficha]:
    """Busca por título. `preciso=True` casa o nome exato — bom para "Ghost of
    Yotei". Para termo de franquia ("Zelda", "Mario") use preciso=False e uma
    plataforma: sem isso o RAWG devolve um indie chamado ZELDA antes do jogo da
    Nintendo, porque o casamento exato de string ganha da relevância."""
    params: dict[str, Any] = {"search": titulo, "page_size": max(limite, 20)}
    if preciso:
        params["search_precise"] = "true"
    else:
        params["ordering"] = "-added"
    pid = PLATAFORMAS.get(plataforma.lower())
    if pid:
        params["platforms"] = pid

    d = _get("games", **params)
    fichas = [_ficha(g) for g in d.get("results", [])]
    if not preciso:
        fichas.sort(key=lambda f: f.relevancia, reverse=True)
    return fichas[:limite]


def top(plataforma: str, min_metacritic: int = 80, limite: int = 20,
        desde: str = "") -> list[Ficha]:
    """Os maiores da plataforma: nota alta E muita gente jogando.

    Ordenamos por popularidade na API (ela não sabe ordenar pela nossa fórmula)
    e reordenamos por `relevancia` aqui, depois de filtrar pela nota mínima.
    """
    pid = PLATAFORMAS.get(plataforma.lower())
    if not pid:
        return []
    params: dict[str, Any] = {
        "platforms": pid, "ordering": "-added",
        "page_size": min(40, max(limite * 3, 20)),
        "metacritic": f"{min_metacritic},100",
    }
    if desde:
        params["dates"] = f"{desde},2030-12-31"
    d = _get("games", **params)
    fichas = [_ficha(g) for g in d.get("results", [])]
    fichas.sort(key=lambda f: f.relevancia, reverse=True)
    return fichas[:limite]
