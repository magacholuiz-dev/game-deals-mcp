"""Shopee Brasil — Affiliate Open API (GraphQL assinado).

Caminho legitimo: cadastre-se no Programa de Afiliados Shopee
(affiliate.shopee.com.br) e pegue App ID / App Secret na area de Open API.
Sem isso o provider fica inerte.

Assinatura: SHA256(appid + timestamp + payload + secret).
"""
from __future__ import annotations

import hashlib
import json
import time

from ..models import Listing, Offer
from .. import config
from .base import cents, client

ENDPOINT = "https://open-api.affiliate.shopee.com.br/graphql"

_SEARCH_Q = """
query($kw: String!, $limit: Int!) {
  productOfferV2(keyword: $kw, limit: $limit, sortType: 2) {
    nodes { itemId productName price priceMin priceMax offerLink
            productLink shopName sales ratingStar priceDiscountRate }
  }
}
"""

_ITEM_Q = """
query($ids: String!) {
  productOfferV2(itemId: $ids, limit: 1) {
    nodes { itemId productName price priceMin priceMax offerLink imageUrl
            productLink shopName sales ratingStar priceDiscountRate }
  }
}
"""


class Shopee:
    name = "shopee"
    label = "Shopee Brasil (Afiliados)"

    def configured(self) -> bool:
        return bool(config.SHOPEE_APP_ID and config.SHOPEE_APP_SECRET)

    def why_unconfigured(self) -> str:
        return ("defina SHOPEE_APP_ID e SHOPEE_APP_SECRET — exige cadastro no "
                "Programa de Afiliados Shopee (Open API)")

    def _post(self, query: str, variables: dict) -> dict:
        payload = json.dumps({"query": query, "variables": variables},
                             separators=(",", ":"))
        ts = int(time.time())
        raw = f"{config.SHOPEE_APP_ID}{ts}{payload}{config.SHOPEE_APP_SECRET}"
        sig = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "Authorization": (f"SHA256 Credential={config.SHOPEE_APP_ID}, "
                              f"Timestamp={ts}, Signature={sig}"),
        }
        with client(headers=headers) as c:
            r = c.post(ENDPOINT, content=payload)
            if r.status_code >= 400:
                return {"_error": r.status_code, "_body": r.text[:400]}
            return r.json() or {}

    @staticmethod
    def _nodes(data: dict) -> list[dict]:
        return (((data.get("data") or {}).get("productOfferV2") or {})
                .get("nodes") or [])

    def search(self, query: str, limit: int = 10) -> list[Listing]:
        if not self.configured():
            return []
        data = self._post(_SEARCH_Q, {"kw": query, "limit": min(limit, 50)})
        return [
            Listing(source=self.name, source_id=str(n.get("itemId")),
                    title=n.get("productName", ""),
                    url=n.get("offerLink") or n.get("productLink", ""),
                    image=n.get("imageUrl", ""),
                    price_cents=cents(n.get("price")),
                    extra={"loja": n.get("shopName"), "vendas": n.get("sales"),
                           "nota": n.get("ratingStar")})
            for n in self._nodes(data) if n.get("itemId")
        ]

    def fetch(self, source_id: str) -> list[Offer]:
        """source_id = itemId da Shopee."""
        if not self.configured():
            return []
        data = self._post(_ITEM_Q, {"ids": str(source_id)})
        out: list[Offer] = []
        for n in self._nodes(data):
            price_c = cents(n.get("price")) or cents(n.get("priceMin"))
            if price_c is None:
                continue
            # priceDiscountRate vem em % (ex: 23). Reconstroi o preco cheio.
            rate = n.get("priceDiscountRate")
            regular = None
            try:
                if rate and float(rate) > 0:
                    regular = int(round(price_c / (1 - float(rate) / 100)))
            except (TypeError, ValueError, ZeroDivisionError):
                regular = None
            out.append(Offer(
                source=self.name, source_id=str(n.get("itemId", source_id)),
                title=n.get("productName", ""), store="Shopee",
                price_cents=price_c, regular_cents=regular,
                currency=config.CURRENCY,
                url=n.get("offerLink") or n.get("productLink", ""),
                image=n.get("imageUrl", ""),
                seller=n.get("shopName", ""),
                extra={"vendas": n.get("sales"), "nota": n.get("ratingStar")},
            ))
        return out


provider = Shopee()
