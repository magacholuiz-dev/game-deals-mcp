"""IsThereAnyDeal — jogos de PC. A peca mais valiosa: ja vem com historico,
entao voce nasce com veredito no dia 1 em vez de esperar 2 meses coletando.

Chave gratuita em https://isthereanydeal.com/apps/my/
"""
from __future__ import annotations

from ..models import Listing, Offer
from .. import config
from .base import cents, client

BASE = "https://api.isthereanydeal.com"


class ITAD:
    name = "itad"
    label = "IsThereAnyDeal (PC)"

    def configured(self) -> bool:
        return bool(config.ITAD_API_KEY)

    def why_unconfigured(self) -> str:
        return "defina ITAD_API_KEY no .env (chave gratuita em isthereanydeal.com/apps/my)"

    def search(self, query: str, limit: int = 10) -> list[Listing]:
        if not self.configured():
            return []
        with client() as c:
            r = c.get(f"{BASE}/games/search/v2",
                      params={"key": config.ITAD_API_KEY, "title": query,
                              "results": limit})
            r.raise_for_status()
            data = r.json() or []
        return [
            Listing(source=self.name, source_id=g.get("id", ""),
                    title=g.get("title", ""),
                    url=f"https://isthereanydeal.com/game/{g.get('slug','')}/info/",
                    extra={"type": g.get("type")})
            for g in data if g.get("id")
        ]

    def fetch(self, source_id: str) -> list[Offer]:
        """source_id = UUID do jogo no ITAD. Devolve uma oferta por loja."""
        if not self.configured():
            return []
        offers: list[Offer] = []
        with client() as c:
            r = c.post(f"{BASE}/games/prices/v2",
                       params={"key": config.ITAD_API_KEY, "country": config.COUNTRY,
                               "capacity": 12, "nondeals": "false"},
                       json=[source_id])
            r.raise_for_status()
            for game in r.json() or []:
                for d in game.get("deals", []) or []:
                    price = (d.get("price") or {})
                    regular = (d.get("regular") or {})
                    amt = price.get("amountInt")
                    if amt is None:
                        amt = cents(price.get("amount"))
                    if amt is None:
                        continue
                    shop = (d.get("shop") or {}).get("name", "?")
                    offers.append(Offer(
                        source=self.name, source_id=source_id,
                        title=shop, store=shop,
                        price_cents=int(amt),
                        regular_cents=regular.get("amountInt") or cents(regular.get("amount")),
                        currency=price.get("currency", config.CURRENCY),
                        url=d.get("url", ""),
                        extra={"drm": d.get("drm"), "cut": d.get("cut")},
                    ))
        return offers

    def seed_history(self, source_id: str, since_iso: str | None = None) -> list[dict]:
        """Log historico de precos — usado para semear o SQLite no dia 1."""
        if not self.configured():
            return []
        params = {"key": config.ITAD_API_KEY, "id": source_id,
                  "country": config.COUNTRY}
        if since_iso:
            params["since"] = since_iso
        with client() as c:
            r = c.get(f"{BASE}/games/history/v2", params=params)
            if r.status_code >= 400:
                return []
            return r.json() or []


provider = ITAD()
