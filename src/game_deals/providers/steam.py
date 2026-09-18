"""Steam BR — endpoint publico da loja. Sem chave, sem cadastro."""
from __future__ import annotations

from ..models import Listing, Offer
from .. import config
from .base import client


class Steam:
    name = "steam"
    label = "Steam (Brasil)"

    def configured(self) -> bool:
        return True

    def why_unconfigured(self) -> str:
        return ""

    def search(self, query: str, limit: int = 10) -> list[Listing]:
        with client() as c:
            r = c.get("https://store.steampowered.com/api/storesearch/",
                      params={"term": query, "l": "portuguese", "cc": config.COUNTRY})
            if r.status_code >= 400:
                return []
            items = (r.json() or {}).get("items", [])[:limit]
        return [
            Listing(source=self.name, source_id=str(i.get("id")),
                    title=i.get("name", ""),
                    url=f"https://store.steampowered.com/app/{i.get('id')}/",
                    image=i.get("tiny_image", ""),
                    price_cents=(i.get("price") or {}).get("final"))
            for i in items if i.get("id")
        ]

    def fetch(self, source_id: str) -> list[Offer]:
        """source_id = appid."""
        with client() as c:
            r = c.get("https://store.steampowered.com/api/appdetails",
                      params={"appids": source_id, "cc": config.COUNTRY, "l": "pt"})
            if r.status_code >= 400:
                return []
            payload = (r.json() or {}).get(str(source_id)) or {}
        if not payload.get("success"):
            return []
        d = payload.get("data") or {}
        po = d.get("price_overview")
        if not po:
            return []   # gratuito ou sem preco na regiao
        return [Offer(
            source=self.name, source_id=str(source_id),
            title=d.get("name", ""), store="Steam",
            price_cents=int(po.get("final", 0)),
            regular_cents=po.get("initial"),
            currency=po.get("currency", config.CURRENCY),
            url=f"https://store.steampowered.com/app/{source_id}/",
            image=d.get("header_image", ""),
        )]


provider = Steam()
