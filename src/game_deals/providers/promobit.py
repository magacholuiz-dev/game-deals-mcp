"""Promobit: community deals, read from the public listing page only.

WHAT CHANGED AND WHY
The first version of this provider called api.promobit.com.br/search. That host's
robots.txt is explicit for every crawler:

    User-Agent: *
    Allow: /v4/redirect/
    Disallow: /

with a comment saying the API is closed by default and only the link-preview
route is meant to be read. The project promises to obey robots.txt, so that
integration was wrong and has been removed. The HTTP client now refuses the host
on its own (see tests, which use the recorded robots.txt).

WHAT REMAINS
www.promobit.com.br allows /promocoes/games/ (its robots.txt closes /buscar*,
/api/*, /v2* and a few others, but not this). The page is server rendered and its
__NEXT_DATA__ carries the current offers. Limits, measured on 2026-09-21:

- about 12 offers per page, mostly hardware and gift cards;
- the `?page=` parameter is ignored by the server (pagination is client side);
- platform sub-listings such as /promocoes/games/playstation-5/ answer 404;
- no search by title (that is /buscar, disallowed) and no finished offers.

So coverage is far smaller than before: this feed can notice a deal that happens
to be on the games front page, nothing more. Anything that must be tracked
reliably needs a store provider or an affiliate feed. Matching is done locally
with matching.py, because the site cannot be searched.

It is a "feed": what it returns goes to deal_signals, never to price_points.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from .. import http
from ..matching import match_offer, price_band, within_band
from ..models import Listing
from .base import cents

SITE = "https://www.promobit.com.br"
LISTING_URL = f"{SITE}/promocoes/games/"
IMG = "https://i.promobit.com.br/400"
LISTING_TTL = 30 * 60           # the page changes through the day, not by the second

_NEXT = re.compile(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


@dataclass
class Sinal:
    """One posted offer: a signal, not an observed store price."""
    source: str
    source_id: str
    titulo: str
    loja: str
    price_cents: int
    old_price_cents: int | None
    desconto_pct: float
    url: str
    imagem: str
    publicado_ts: int
    curtidas: int
    comentarios: int
    ativa: bool = True
    categoria: str = ""
    subcategoria: str = ""

    def dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["preco"] = self.price_cents / 100
        return d


def _ts(value: str) -> int:
    try:
        return int(dt.datetime.fromisoformat(value).timestamp()) if value else 0
    except ValueError:
        return 0


def parse_listing(html: str) -> list[Sinal]:
    """Offers from the listing page state. Empty list if the format changed."""
    m = _NEXT.search(html)
    if not m:
        return []
    try:
        state = json.loads(m.group(1))
        offers = state["props"]["pageProps"]["serverOffers"]["offers"]
    except (ValueError, KeyError, TypeError):
        return []

    out: list[Sinal] = []
    for o in offers:
        price = cents(o.get("offerPrice"))
        if not price:
            continue
        old = cents(o.get("offerOldPrice"))
        slug = o.get("offerSlug", "")
        photo = o.get("offerPhoto") or ""
        out.append(Sinal(
            source="promobit", source_id=str(o.get("offerId", "")),
            titulo=o.get("offerTitle", ""), loja=o.get("storeName") or "?",
            price_cents=price, old_price_cents=old or None,
            desconto_pct=float(o.get("offerDiscontPercentage") or 0),
            url=f"{SITE}/oferta/{slug}" if slug else SITE,
            imagem=f"{IMG}{photo}" if photo.startswith("/") else "",
            publicado_ts=_ts(o.get("offerPublished", "")),
            curtidas=int(o.get("offerLikes") or 0),
            comentarios=int(o.get("offerComments") or 0),
            categoria=o.get("categoryName") or "",
            subcategoria=o.get("subcategoryName") or ""))
    return out


# How the site labels a platform in `subcategoryName`, keyed by our short names.
# Switch 2 listings are filed under "Nintendo Switch" there.
PLATFORM_LABEL = {"ps4": "playstation 4", "ps5": "playstation 5",
                  "switch": "nintendo switch", "switch2": "nintendo switch",
                  "xbox": "xbox", "pc": "pc"}


def _platform_ok(subcategory: str, platform: str) -> bool:
    if not subcategory:
        return True                     # unlabeled: do not exclude on a guess
    label = PLATFORM_LABEL.get(platform.lower(), platform.lower())
    return label in subcategory.lower()


class Promobit:
    name = "promobit"
    label = "Promobit (listagem pública de games)"
    kind = "feed"               # never feeds the price history

    def __init__(self) -> None:
        self.last_error = ""

    def configured(self) -> bool:
        return True

    def why_unconfigured(self) -> str:
        return ""

    def listing(self) -> list[Sinal]:
        self.last_error = ""
        try:
            r = http.get(LISTING_URL, ttl=LISTING_TTL)
        except http.RobotsBlocked as e:
            self.last_error = str(e)
            return []
        if r.status_code != 200:
            self.last_error = f"{LISTING_URL} answered {r.status_code}"
            return []
        sinais = parse_listing(r.text)
        if not sinais:
            self.last_error = "listing page had no offers (format changed?)"
        return sinais

    def sinais_do_titulo(self, titulo: str, limit: int = 12, plataforma: str = "",
                         incluir_encerradas: bool = False,
                         ignorar_acessorios: bool = True) -> list[Sinal]:
        """Offers on the listing that ARE this title.

        `incluir_encerradas` is accepted for compatibility and ignored: finished
        offers were only available from the closed API. `ignorar_acessorios` is
        the negative-anchor filter; it is always applied except when the title
        asked for is itself an accessory (matching.negative_hits exempts words
        that appear in the query)."""
        matched = [s for s in self.listing() if match_offer(s.titulo, titulo).ok]
        if plataforma:
            matched = [s for s in matched if _platform_ok(s.subcategoria, plataforma)]
        band = price_band([s.price_cents for s in matched])
        matched = [s for s in matched if within_band(s.price_cents, band)]
        return sorted(matched, key=lambda s: s.price_cents)[:limit]

    def sinais(self, query: str, limit: int = 20,
               incluir_encerradas: bool = False) -> list[Sinal]:
        return self.sinais_do_titulo(query, limit)

    def search(self, query: str, limit: int = 10) -> list[Listing]:
        return [Listing(source=self.name, source_id=s.source_id, title=s.titulo,
                        url=s.url, image=s.imagem, price_cents=s.price_cents,
                        extra={"loja": s.loja, "desconto": s.desconto_pct})
                for s in self.sinais_do_titulo(query, limit)]

    def fetch(self, source_id: str) -> list:
        """A post is an event, not a series; the collector uses sinais_do_titulo."""
        return []


provider = Promobit()
