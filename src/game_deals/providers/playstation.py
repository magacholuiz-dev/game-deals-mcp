"""PlayStation Store (BR).

A PS Store nao tem API publica: o GraphQL dela e whitelist de persisted query
(hash que rotaciona). O caminho durável e o **JSON-LD** que a propria pagina
publica — schema.org, existe justamente para ser lido por maquina, e traz nome,
imagem e preco em BRL. O preco cheio (para calcular o desconto) vem do estado do
micro-frontend na mesma resposta, como melhor-esforco: se a Sony mudar aquele
bloco, o provider continua funcionando, so perde o "de/por".

Uma requisicao por produto rastreado por dia — volume de watchlist pessoal.

source_id aceita:
    "10000493"                          -> /pt-br/concept/10000493
    "concept/10000493"
    "product/UP2014-CUSA18793_00-..."
"""
from __future__ import annotations

import json
import re

from ..models import Listing, Offer
from .. import config
from .base import cents, client

BASE = "https://store.playstation.com"
LOCALE = "pt-br"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

_JSONLD = re.compile(r'id="mfe-jsonld-tags"[^>]*>(.*?)</script>', re.S)
_PRICEBLK = re.compile(
    r'"basePriceValue":(\d+),"discountedValue":(\d+),"currencyCode":"(\w+)"')


def _path(source_id: str) -> str:
    sid = source_id.strip().strip("/")
    if sid.startswith(("concept/", "product/")):
        return f"/{LOCALE}/{sid}"
    kind = "concept" if sid.isdigit() else "product"
    return f"/{LOCALE}/{kind}/{sid}"


class PlayStation:
    name = "playstation"
    label = "PlayStation Store (BR)"

    def configured(self) -> bool:
        return True

    def why_unconfigured(self) -> str:
        return ""

    def search(self, query: str, limit: int = 10) -> list[Listing]:
        """A busca da PS Store e uma SPA — nao ha HTML para ler sem simular o app.
        Descubra o jogo pelo `discover_top` (RAWG) ou cole a URL da loja, e use o
        id do caminho como source_id."""
        return []

    def fetch(self, source_id: str) -> list[Offer]:
        with client(headers={"User-Agent": UA,
                             "Accept": "text/html,application/xhtml+xml"}) as c:
            r = c.get(BASE + _path(source_id))
            if r.status_code >= 400:
                return []
            html = r.text

        m = _JSONLD.search(html)
        if not m:
            return []
        try:
            d = json.loads(m.group(1))
        except json.JSONDecodeError:
            return []
        if isinstance(d, list):
            d = next((x for x in d if x.get("@type") == "Product"), {})

        offer = d.get("offers") or {}
        price_c = cents(offer.get("price"))
        if price_c is None:
            return []

        # "de/por": melhor-esforco sobre o estado do micro-frontend.
        regular_c = None
        pm = _PRICEBLK.search(html)
        if pm:
            base, disc = int(pm.group(1)), int(pm.group(2))
            if base > disc:
                regular_c = base
                price_c = disc

        return [Offer(
            source=self.name, source_id=source_id,
            title=d.get("name", ""), store="PlayStation Store",
            price_cents=price_c, regular_cents=regular_c,
            currency=(offer.get("priceCurrency") or config.CURRENCY),
            url=BASE + _path(source_id),
            image=d.get("image", ""),
            extra={"categoria": d.get("category"), "sku": d.get("sku")},
        )]


provider = PlayStation()
