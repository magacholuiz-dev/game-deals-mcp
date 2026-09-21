"""PlayStation Store BR: editions, sale prices and sale end dates.

Everything here was verified against live pages and is pinned by fixtures
(tests/fixtures/playstation/, recorded by scripts/probe.py).

What the store gives us
- Product and concept pages are server rendered. Each one embeds an Apollo cache
  in <script id="env:..."> tags: `Product:` entries (name, edition), `Sku:`
  entries and `GameCTA:` entries. The CTA of a sku holds its price object:
  basePriceValue and discountedValue in cents, and `endTime`, the sale end as a
  string of epoch MILLISECONDS.
- A concept page lists every edition. The JSON-LD on it only describes ONE of
  them (the default), so reading the JSON-LD alone returns the wrong edition's
  price for a concept with several. That was the old provider's bug.
- Some CTAs are not purchases: "Incluído" (PS Plus catalog, discountedValue 0)
  and the free demo. They must not be read as the game's price.

What the store does NOT give us: discovery. Category and deals pages
(/pt-br/category/<uuid>/N, /pt-br/pages/deals) return an identical client-side
rendered shell with no products, checked for three different categories, and
there is no sitemap (those URLs return the app shell too). The data behind them
is fetched with GraphQL persisted queries whose hashes rotate. Finding what to
track therefore has to come from elsewhere (RAWG, a store search of another
retailer, your own wishlist). `discover_category` exists so that, if Sony ever
server-renders those pages, the code path is already tested against the shell.

Editions never fall back to each other. Asking for "#deluxe" on a game without
a Deluxe edition returns nothing and says which editions exist. A bare concept
id with more than one sellable edition is ambiguous and is refused for the same
reason.

source_id grammar
    "10003386#deluxe"      concept 10003386, Deluxe edition
    "concept/10003386"     concept, must have a single sellable edition
    "product/UP9000-..."   one specific product id (unambiguous)
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from typing import Any

from .. import config, http
from ..models import Listing, Offer

BASE = "https://store.playstation.com"
LOCALE = "pt-br"

_JSONLD = re.compile(r'<script[^>]*id="mfe-jsonld-tags"[^>]*>(.*?)</script>', re.S)
_ENV = re.compile(r'<script[^>]*id="env:[^"]+"[^>]*>(.*?)</script>', re.S)
_NEXT = re.compile(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

STANDARD, DELUXE, ULTIMATE, SPECIAL = "standard", "deluxe", "ultimate", "special"
SELLABLE = (STANDARD, DELUXE, ULTIMATE, SPECIAL)
_ORDER = {t: i for i, t in enumerate(SELLABLE)}


@dataclass(frozen=True)
class Edition:
    product_id: str
    name: str
    tier: str                       # standard | deluxe | ultimate | special | upgrade | demo | addon
    price_cents: int | None         # what you pay now
    regular_cents: int | None       # full price when it is on sale
    sale_end_ts: int | None         # epoch seconds
    ps_plus_included: bool
    free: bool = False

    @property
    def sellable(self) -> bool:
        return self.tier in SELLABLE and self.price_cents is not None

    @property
    def on_sale(self) -> bool:
        return self.regular_cents is not None

    @property
    def sale_end_iso(self) -> str | None:
        if self.sale_end_ts is None:
            return None
        return dt.datetime.fromtimestamp(self.sale_end_ts, dt.timezone.utc) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class PsPage:
    concept_id: str
    name: str
    editions: list[Edition]

    def sellable(self) -> list[Edition]:
        return sorted((e for e in self.editions if e.sellable),
                      key=lambda e: _ORDER[e.tier])


# ----------------------------------------------------------------- parsing

def _cache(html: str) -> dict[str, Any]:
    cache: dict[str, Any] = {}
    for block in _ENV.findall(html):
        try:
            cache.update(json.loads(block).get("cache") or {})
        except (ValueError, AttributeError):
            continue
    return cache


def edition_tier(product_id: str, name: str, edition: str, kind: str) -> str:
    """Order matters: "ULTEDTIONUPGRADE" contains "ULT", and an upgrade pack is
    not an edition you can buy on its own."""
    blob = f"{product_id} {name} {edition} {kind}".lower()
    if "demo" in blob or "trial" in blob:
        return "demo"
    if any(w in blob for w in ("upgrade", "melhoria", "atualiza", "pacote de expans")):
        return "upgrade"
    if "ultimate" in blob:
        return ULTIMATE
    if "deluxe" in blob:
        return DELUXE
    if any(w in product_id.lower() for w in ("standard", "fullgame", "main")) \
            or "standard" in blob or "padr" in blob:
        return STANDARD
    if edition:
        return SPECIAL          # anniversary, collector, ...
    return STANDARD if name == "" else "addon"


def parse_page(html: str) -> PsPage | None:
    cache = _cache(html)
    if not cache:
        return None
    ld: dict[str, Any] = {}
    m = _JSONLD.search(html)
    if m:
        try:
            ld = json.loads(m.group(1))
        except ValueError:
            ld = {}

    concept_id, concept_name = "", ""
    for key, val in cache.items():
        if key.startswith("Concept:") and isinstance(val, dict):
            concept_id = key.split(":", 1)[1]
            concept_name = val.get("name") or ""
    concept_name = concept_name or ld.get("name") or ""

    # sku id -> (purchase price object, ps_plus_included, is_demo)
    prices: dict[str, dict[str, Any]] = {}
    plus: set[str] = set()
    for key, val in cache.items():
        if not key.startswith("GameCTA:") or not isinstance(val, dict):
            continue
        parts = key.split(":")
        if len(parts) < 4:
            continue
        cta_type, sku = parts[1], parts[3]
        pid = re.sub(r"-U\d+$", "", sku)
        price = val.get("price") or {}
        brands = price.get("serviceBranding") or []
        if "PS_PLUS" in brands or "UPSELL" in cta_type:
            plus.add(pid)               # "Incluído": not a purchase price
            continue
        if "TRIAL" in cta_type or price.get("basePriceValue") is None:
            continue
        cur = prices.get(pid)
        if cur is None or price.get("discountedValue", 10**9) < cur.get("discountedValue", 10**9):
            prices[pid] = price

    editions: list[Edition] = []
    for key, val in cache.items():
        if not key.startswith("Product:") or not isinstance(val, dict):
            continue
        pid = key.split(":", 1)[1]
        edition_obj = val.get("edition")
        edition_name = (edition_obj or {}).get("name", "") if isinstance(edition_obj, dict) else ""
        name = val.get("name") or ""
        tier = edition_tier(pid, name, edition_name,
                            val.get("localizedStoreDisplayClassification") or "")
        pr = prices.get(pid)
        price_c = regular_c = end_ts = None
        free = False
        if pr:
            base, disc = pr.get("basePriceValue"), pr.get("discountedValue")
            price_c = disc if disc is not None else base
            free = bool(pr.get("isFree")) or price_c == 0
            if base is not None and disc is not None and disc < base:
                regular_c = base
                end = pr.get("endTime")
                end_ts = int(end) // 1000 if end not in (None, "") else None
        editions.append(Edition(
            product_id=pid, name=name or concept_name, tier=tier,
            price_cents=price_c, regular_cents=regular_c, sale_end_ts=end_ts,
            ps_plus_included=pid in plus, free=free))
    return PsPage(concept_id, concept_name, editions)


def parse_category_shell(html: str) -> list[str]:
    """Concept ids found on a category page. The live pages are an empty shell,
    so this returns [] today; kept so a future server-rendered page is picked up."""
    ids: list[str] = []
    ids += re.findall(r"/concept/(\d+)", html)
    m = _NEXT.search(html)
    if m:
        ids += re.findall(r'"conceptId"\s*:\s*"?(\d+)', m.group(1))
    return list(dict.fromkeys(ids))


# ----------------------------------------------------------------- provider

def _split(source_id: str) -> tuple[str, str, str]:
    """-> (kind, id, edition). kind is 'concept' or 'product'."""
    sid, _, edition = source_id.strip().partition("#")
    sid = sid.strip("/")
    if sid.startswith("product/"):
        return "product", sid.split("/", 1)[1], edition.lower()
    if sid.startswith("concept/"):
        sid = sid.split("/", 1)[1]
    return ("concept" if sid.isdigit() else "product"), sid, edition.lower()


class PlayStation:
    name = "playstation"
    label = "PlayStation Store (BR)"

    def __init__(self) -> None:
        self.last_error = ""

    def configured(self) -> bool:
        return True

    def why_unconfigured(self) -> str:
        return ""

    def _page(self, kind: str, ident: str) -> PsPage | None:
        url = f"{BASE}/{LOCALE}/{kind}/{ident}"
        try:
            r = http.get(url)
        except http.RobotsBlocked as e:
            self.last_error = str(e)
            return None
        if r.status_code != 200:
            self.last_error = f"{url} answered {r.status_code}"
            return None
        page = parse_page(r.text)
        if page is None:
            self.last_error = f"{url}: no product state found (format changed?)"
        return page

    def editions(self, source_id: str) -> list[Edition]:
        kind, ident, _ = _split(source_id)
        page = self._page(kind, ident)
        return page.sellable() if page else []

    def search(self, query: str, limit: int = 10) -> list[Listing]:
        """The store search is a client-side app with nothing to read."""
        return []

    def discover_category(self, category_id: str, page: int = 1) -> list[Listing]:
        url = f"{BASE}/{LOCALE}/category/{category_id}/{page}"
        try:
            r = http.get(url)
        except http.RobotsBlocked as e:
            self.last_error = str(e)
            return []
        ids = parse_category_shell(r.text) if r.status_code == 200 else []
        if not ids:
            self.last_error = ("category pages are client-side rendered: no "
                               "products in the HTML (verified 2026-09-21)")
        return [Listing(source=self.name, source_id=i, title="",
                        url=f"{BASE}/{LOCALE}/concept/{i}") for i in ids]

    def fetch(self, source_id: str) -> list[Offer]:
        self.last_error = ""
        kind, ident, wanted = _split(source_id)
        page = self._page(kind, ident)
        if page is None:
            return []
        sellable = page.sellable()
        if not sellable:
            self.last_error = f"{source_id}: no purchasable edition found"
            return []
        by_tier = ", ".join(e.tier for e in sellable)

        if kind == "product":
            chosen = next((e for e in sellable if e.product_id == ident), None)
            if chosen is None:
                self.last_error = f"product {ident} is not sold here; page has: {by_tier}"
                return []
        elif wanted:
            chosen = next((e for e in sellable if e.tier == wanted), None)
            if chosen is None:
                # Never fall back to another edition: that would record the
                # Standard price under a Deluxe product.
                self.last_error = (f"no {wanted!r} edition on concept {ident}; "
                                   f"available: {by_tier}")
                return []
        elif len(sellable) == 1:
            chosen = sellable[0]
        else:
            self.last_error = (f"concept {ident} has several editions ({by_tier}); "
                               f"say which with '{ident}#<edition>' or product/<id>")
            return []

        url = (f"{BASE}/{LOCALE}/concept/{ident}" if kind == "concept"
               else f"{BASE}/{LOCALE}/product/{ident}")
        assert chosen.price_cents is not None
        return [Offer(
            source=self.name, source_id=source_id, title=chosen.name,
            store="PlayStation Store", price_cents=chosen.price_cents,
            regular_cents=chosen.regular_cents, currency=config.CURRENCY, url=url,
            extra={"edition": chosen.tier, "product_id": chosen.product_id,
                   "promo_ate": chosen.sale_end_iso,
                   "sale_end_ts": chosen.sale_end_ts,
                   "ps_plus_included": chosen.ps_plus_included,
                   "editions_on_page": [e.tier for e in sellable]})]


provider = PlayStation()
