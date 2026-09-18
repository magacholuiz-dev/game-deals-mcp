from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Listing:
    """Um resultado de busca: ainda nao e preco, e um candidato a alias."""
    source: str
    source_id: str
    title: str
    url: str = ""
    image: str = ""
    price_cents: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Offer:
    """Preco observado agora, numa loja especifica."""
    source: str
    source_id: str
    title: str
    price_cents: int
    currency: str = "BRL"
    url: str = ""
    image: str = ""
    in_stock: bool = True
    seller: str = ""
    regular_cents: int | None = None
    store: str = ""          # loja dentro da fonte (ex: "Nuuvem" dentro do ITAD)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def cut_pct(self) -> int:
        if not self.regular_cents or self.regular_cents <= 0:
            return 0
        return round(100 * (1 - self.price_cents / self.regular_cents))

    def dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["price"] = self.price_cents / 100
        d["cut_pct"] = self.cut_pct
        return d


def brl(cents: int | None) -> str:
    if cents is None:
        return "-"
    return f"R$ {cents / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
