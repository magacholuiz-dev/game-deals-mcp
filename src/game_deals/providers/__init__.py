"""Registro de fontes. Cada provider se auto-declara configurado ou nao;
o sistema roda com o subconjunto que tiver credencial."""
from __future__ import annotations

from .base import Provider
from . import (amazon, itad, mercadolivre, nintendo, playstation,
               promobit, shopee, steam)

ALL: dict[str, Provider] = {
    p.name: p for p in (
        itad.provider,
        steam.provider,
        nintendo.provider,
        playstation.provider,
        promobit.provider,
        mercadolivre.provider,
        amazon.provider,
        shopee.provider,
    )
}


def get(name: str) -> Provider | None:
    return ALL.get(name)


def available() -> dict[str, Provider]:
    return {n: p for n, p in ALL.items() if p.configured()}


def kind(name: str) -> str:
    """"store" observa preco de uma loja no tempo; "feed" traz oferta postada por
    gente. So os "store" alimentam o historico."""
    p = ALL.get(name)
    return getattr(p, "kind", "store") if p else "store"


def lojas() -> dict[str, Provider]:
    return {n: p for n, p in available().items() if kind(n) == "store"}


def feeds() -> dict[str, Provider]:
    return {n: p for n, p in available().items() if kind(n) == "feed"}


def status() -> list[dict]:
    return [
        {
            "fonte": p.name,
            "nome": p.label,
            "configurado": p.configured(),
            "tipo": getattr(p, "kind", "store"),
            "pendencia": "" if p.configured() else p.why_unconfigured(),
        }
        for p in ALL.values()
    ]
