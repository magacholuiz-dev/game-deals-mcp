"""Nintendo eShop BR: Switch 2, Switch 2 Edition and Switch 1, priced in BRL.

Every fact below was verified against live endpoints and is pinned by fixtures
recorded with scripts/probe.py (see tests/fixtures/MANIFEST.json).

Where each thing comes from
- Prices, sale price and sale end date: api.ec.nintendo.com/v1/price with
  country=BR. This is the only source of the REGULAR price. The JSON-LD on the
  product page shows the CURRENT price, so on a sale it is the promotional one.
- Identity (name and NSUID): the JSON-LD on the pt-br product page. The NSUID is
  in the image URL, /store/software/switch2/<nsuid>/...
- Which pages exist: www.nintendo.com/pt-br/store/sitemap.xml, about 28 thousand
  products, listed in robots.txt. A title missing from it is simply not sold in
  Brazil (007 First Light, at the time of writing), it is not a slug bug.
- Discovery and classification: the European Solr index, the only catalog that
  knows Switch 2 (`system_type` contains "nintendoswitch2").

The trap this module exists to avoid: NSUIDs are regional. The European NSUID of
Mario Kart World (70010000096802) and of No Man's Sky Switch 2 Edition
(70010000099138) return `not_found` with country=BR, while the Brazilian ones
(70010000095431, 70010000099217) price correctly. Solr hands out the European
ones, so an id taken from it must never be trusted.
"""
from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any

from .. import config, http
from ..matching import anchors, has_anchors, negative_hits, normalize, tokens
from ..models import Listing, Offer
from .base import cents

SOLR = "https://search.nintendo-europe.com/en/select"
PRICE_URL = "https://api.ec.nintendo.com/v1/price"
LOJA_BR = "https://www.nintendo.com/pt-br/store/products"
SITEMAP_URL = "https://www.nintendo.com/pt-br/store/sitemap.xml"

ALGOLIA_APP = os.environ.get("NINTENDO_ALGOLIA_APP_ID", "U3B6GR4UA3")
ALGOLIA_KEY = os.environ.get("NINTENDO_ALGOLIA_KEY", "c4da8be7fd29f0f5bfa42920b0a99dc7")
ALGOLIA_INDEX = os.environ.get("NINTENDO_ALGOLIA_INDEX", "ncom_game_pt_br")

NATIVO = "switch2_nativo"
EDICAO = "switch2_edition"
RETRO = "switch1_retrocompativel"

ROTULO = {
    NATIVO: "Switch 2 (nativo)",
    EDICAO: "Switch 2 Edition (upgrade)",
    RETRO: "Switch 1 — roda no Switch 2",
}

_NSUID = re.compile(r"^\d{14}$")
_LD_BLOCK = re.compile(r"<script[^>]*ld\+json[^>]*>(.*?)</script>", re.S)
_NSUID_IN_IMAGE = re.compile(r"/(\d{14})/")
_SITEMAP_SLUG = re.compile(
    r"<loc>https://www\.nintendo\.com/pt-br/store/products/([^/<]+)/?</loc>")


# ------------------------------------------------------------ classification

@dataclass(frozen=True)
class Classification:
    tier: str | None          # None = not a Switch title at all
    upgrade_pack: bool        # a paid upgrade for a Switch 1 base game
    reason: str


def classify(title: str = "", system_type: str = "", slug: str = "") -> Classification:
    """Which of the three tiers a listing belongs to.

    Order matters. A "Switch 2 Edition" also carries system_type
    "nintendoswitch2" in Solr, so the title has to be read first or every
    edition would be reported as native.
    """
    blob = normalize(f"{title} {slug.replace('-', ' ')}")
    upgrade = any(p in blob for p in
                  ("upgrade pack", "pacote de upgrade", "pacote de melhoria"))
    st = (system_type or "").lower()

    if "switch 2 edition" in blob:
        return Classification(EDICAO, upgrade, "title says Switch 2 Edition")
    if st:
        if "nintendoswitch2" in st:
            return Classification(NATIVO, False, "system_type nintendoswitch2")
        if "nintendoswitch" in st:
            return Classification(RETRO, False, "system_type nintendoswitch")
        return Classification(None, False, f"not a Switch title ({st[:40]})")
    if slug.endswith("-switch-2"):
        return Classification(NATIVO, False, "slug ends with -switch-2")
    if slug.endswith("-switch"):
        return Classification(RETRO, False, "slug ends with -switch")
    return Classification(None, False, "cannot tell from the title alone")


def compatibilidade(titulo: str, system_type: str = "") -> str:
    """Legacy helper: always answers with a tier, defaulting to Switch 1."""
    return classify(titulo, system_type).tier or RETRO


# ------------------------------------------------------------------- slugs

def slugify(title: str) -> str:
    t = title.replace("™", "").replace("®", "").replace("©", "")
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    t = t.replace("'", "").replace("’", "")        # "no-mans-sky", not "no-man-s"
    return re.sub(r"-+", "-", re.sub(r"[^a-zA-Z0-9]+", "-", t)).strip("-").lower()


def title_from_slug(slug: str) -> str:
    """Best-effort title for a slug that came without one. The trailing
    platform suffix is not part of the name: left in, "-switch-2" turned into a
    required numeral 2 and rejected the very page the slug points at."""
    return re.sub(r"-switch(?:-2)?$", "", slug).replace("-", " ")


def slug_candidates(title: str, tier: str | None) -> list[str]:
    base = slugify(title)
    if not base:
        return []
    suffixes = {"switch2_nativo": ["switch-2"], "switch2_edition": ["switch-2"],
                "switch1_retrocompativel": ["switch"]}.get(tier or "",
                                                           ["switch-2", "switch"])
    out: list[str] = []
    for suf in suffixes:
        out.append(base if base.endswith(f"-{suf}") else f"{base}-{suf}")
    if tier == EDICAO:
        short = base.replace("nintendo-switch-2-edition", "switch-2-edition")
        out.append(f"{short}-switch-2")
    return list(dict.fromkeys(out))


# ------------------------------------------------------------------ parsing

@dataclass(frozen=True)
class ProductPage:
    name: str
    nsuid: str
    page_price_cents: int | None      # CURRENT price: the promo one during a sale
    available: bool


def parse_product_page(html: str) -> ProductPage | None:
    for block in _LD_BLOCK.findall(html):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        nodes = data.get("@graph") if isinstance(data, dict) and "@graph" in data \
            else [data]
        for node in nodes or []:
            if not isinstance(node, dict) or not node.get("name"):
                continue
            image = node.get("image")
            image = image[0] if isinstance(image, list) and image else image
            m = _NSUID_IN_IMAGE.search(str(image or ""))
            if not m:
                continue
            offer = node.get("offers") or {}
            if isinstance(offer, list):
                offer = offer[0] if offer else {}
            return ProductPage(
                name=str(node["name"]), nsuid=m.group(1),
                page_price_cents=cents(offer.get("price")),
                available="InStock" in str(offer.get("availability", "")))
    return None


def parse_sitemap(xml: str) -> list[str]:
    return _SITEMAP_SLUG.findall(xml)


@dataclass(frozen=True)
class PriceInfo:
    nsuid: str
    status: str                       # onsale | not_found | unreleased | ...
    regular_cents: int | None
    discount_cents: int | None
    discount_end: str | None          # ISO 8601, e.g. 2026-09-24T06:59:59Z

    @property
    def current_cents(self) -> int | None:
        return self.discount_cents if self.discount_cents is not None \
            else self.regular_cents


def parse_prices(payload: dict[str, Any]) -> dict[str, PriceInfo]:
    out: dict[str, PriceInfo] = {}
    for p in payload.get("prices") or []:
        nsuid = str(p.get("title_id", ""))
        reg = (p.get("regular_price") or {}).get("raw_value")
        disc = p.get("discount_price") or {}
        out[nsuid] = PriceInfo(
            nsuid=nsuid, status=p.get("sales_status", ""),
            regular_cents=cents(reg),
            discount_cents=cents(disc.get("raw_value")) if disc else None,
            discount_end=disc.get("end_datetime") if disc else None)
    return out


@dataclass(frozen=True)
class Resolution:
    nsuid: str
    slug: str
    name: str
    tier: str | None
    upgrade_pack: bool
    method: str                       # product_page | sitemap
    price: PriceInfo


# ------------------------------------------------------------------ provider

class Nintendo:
    name = "nintendo"
    label = "Nintendo eShop (Switch / Switch 2)"

    def __init__(self) -> None:
        self.last_error = ""
        self._sitemap: tuple[float, list[str]] | None = None

    def configured(self) -> bool:
        return True

    def why_unconfigured(self) -> str:
        return ""

    # ---- network helpers (all traffic goes through game_deals.http)

    def _get(self, url: str, **kw: Any):
        try:
            return http.get(url, **kw)
        except http.RobotsBlocked as e:
            self.last_error = str(e)
            return None

    def price_info(self, nsuids: list[str]) -> dict[str, PriceInfo]:
        r = self._get(PRICE_URL, params={"country": config.COUNTRY, "lang": "pt",
                                         "ids": ",".join(nsuids)})
        if r is None or r.status_code >= 400:
            return {}
        try:
            return parse_prices(r.json())
        except ValueError:
            return {}

    def _product_page(self, slug: str) -> ProductPage | None:
        r = self._get(f"{LOJA_BR}/{slug}/", ttl=6 * 3600)
        if r is None or r.status_code != 200:
            return None
        return parse_product_page(r.text)

    def sitemap_slugs(self) -> list[str]:
        if self._sitemap and time.time() - self._sitemap[0] < 86400:
            return self._sitemap[1]
        r = self._get(SITEMAP_URL, ttl=86400)
        slugs = parse_sitemap(r.text) if r is not None and r.status_code == 200 else []
        self._sitemap = (time.time(), slugs)
        return slugs

    # ---- resolution: BR product page, then sitemap, always validated

    def _validate(self, slug: str, wanted_title: str, wanted_tier: str | None,
                  method: str) -> Resolution | None:
        page = self._product_page(slug)
        if page is None:
            return None
        query = wanted_title or title_from_slug(slug)

        if not has_anchors(page.name, anchors(query)):
            self.last_error = f"page {slug!r} is {page.name!r}, not {query!r}"
            return None
        if negative_hits(page.name, query):
            self.last_error = f"page {slug!r} looks like an add-on: {page.name!r}"
            return None
        cls = classify(page.name, slug=slug)
        if wanted_tier and cls.tier != wanted_tier:
            self.last_error = (f"page {slug!r} is {cls.tier}, wanted {wanted_tier}")
            return None

        # The decisive check: a European NSUID answers not_found for country=BR.
        info = self.price_info([page.nsuid]).get(page.nsuid)
        if info is None or info.status == "not_found":
            self.last_error = f"NSUID {page.nsuid} does not price in {config.COUNTRY}"
            return None
        return Resolution(page.nsuid, slug, page.name, cls.tier, cls.upgrade_pack,
                          method, info)

    def _sitemap_candidates(self, title: str, tier: str | None) -> list[str]:
        a = anchors(title)
        want = set(tokens(title))
        scored: list[tuple[float, int, str]] = []
        for slug in self.sitemap_slugs():
            text = slug.replace("-", " ")
            if not has_anchors(text, a) or negative_hits(text, title):
                continue
            if tier and classify(slug=slug).tier != tier:
                continue
            got = set(text.split())
            jaccard = len(want & got) / max(1, len(want | got))
            scored.append((-jaccard, len(slug), slug))
        return [s for _, _, s in sorted(scored)[:4]]

    def resolve(self, *, title: str = "", slug: str = "",
                tier: str | None = None) -> Resolution | None:
        """Brazilian NSUID for a title or slug, or None if it is not sold here."""
        if not (title or slug):
            return None
        if tier is None:
            tier = classify(title, slug=slug).tier
        tried: list[str] = []
        first = ([slug] if slug else []) + (slug_candidates(title, tier) if title else [])
        for s in dict.fromkeys(first):                                  # step 1
            tried.append(s)
            r = self._validate(s, title, tier, "product_page")
            if r:
                return r
        for s in self._sitemap_candidates(title or title_from_slug(slug), tier):
            if s in tried:                                              # step 2
                continue
            r = self._validate(s, title, tier, "sitemap")
            if r:
                return r
        return None

    # ---- Provider interface

    def search(self, query: str, limit: int = 10,
               apenas_switch2: bool = False) -> list[Listing]:
        """Discovery through the European Solr index. It does NOT rank by
        popularity: `hits_i` is 300 for every title, so sorting on it would
        produce an arbitrary list that only looks like a ranking."""
        fq = "type:GAME" + (" AND system_type:*switch2*" if apenas_switch2 else "")
        r = self._get(SOLR, params={"q": query or "*", "fq": fq, "wt": "json",
                                    "rows": min(limit, 40)})
        if r is None or r.status_code >= 400:
            return []
        try:
            docs = (r.json().get("response") or {}).get("docs", [])
        except ValueError:
            return []
        out: list[Listing] = []
        for d in docs:
            title = d.get("title", "")
            st = (d.get("system_type") or [""])[0]
            cls = classify(title, st)
            if cls.tier is None:                # Game Boy, SNES, Wii U, ...
                continue
            cands = slug_candidates(title, cls.tier)
            out.append(Listing(
                source=self.name, source_id=cands[0] if cands else "",
                title=title, url=f"{LOJA_BR}/{cands[0]}/" if cands else "",
                image=_image(d),
                extra={"compat": cls.tier, "compat_rotulo": ROTULO[cls.tier],
                       "upgrade_pack": cls.upgrade_pack,
                       "publisher": d.get("publisher"),
                       "lancamento": d.get("pretty_date_s"),
                       # Informational only: never a valid id for country=BR.
                       "nsuid_eu": (d.get("nsuid_txt") or [None])[0]}))
        return out

    def search_br(self, query: str, limit: int = 10) -> list[Listing]:
        """pt-br Algolia catalog. Returns Americas NSUIDs (they price in BRL) but
        predates the Switch 2: none of its titles are Switch 2."""
        url = f"https://{ALGOLIA_APP}-dsn.algolia.net/1/indexes/{ALGOLIA_INDEX}/query"
        try:
            r = http.request("POST", url, json={"query": query, "hitsPerPage": limit},
                             headers={"X-Algolia-API-Key": ALGOLIA_KEY,
                                      "X-Algolia-Application-Id": ALGOLIA_APP})
        except http.RobotsBlocked:
            return []
        if r.status_code >= 400:
            return []
        out = []
        for h in (r.json() or {}).get("hits", []):
            if not h.get("nsuid"):
                continue
            u = h.get("url", "")
            out.append(Listing(
                source=self.name, source_id=str(h["nsuid"]), title=h.get("title", ""),
                url=f"https://www.nintendo.com{u}" if u.startswith("/") else u,
                image=h.get("horizontalHeaderImage", ""),
                extra={"compat": RETRO, "compat_rotulo": ROTULO[RETRO]}))
        return out

    def fetch(self, source_id: str) -> list[Offer]:
        """`source_id` is a Brazilian NSUID (14 digits) or a pt-br product slug."""
        sid = source_id.strip()
        self.last_error = ""
        if _NSUID.match(sid):
            info = self.price_info([sid]).get(sid)
            if info is None or info.status in ("not_found", "unreleased"):
                self.last_error = (f"NSUID {sid} does not price in {config.COUNTRY}; "
                                   "European NSUIDs are not valid here")
                return []
            return [self._offer(sid, sid, "", None, False, "nsuid", info)]
        res = self.resolve(slug=sid)
        if res is None:
            return []
        return [self._offer(sid, res.slug, res.name, res.tier, res.upgrade_pack,
                            res.method, res.price, res.nsuid)]

    def _offer(self, source_id: str, slug: str, name: str, tier: str | None,
               upgrade: bool, method: str, info: PriceInfo,
               nsuid: str = "") -> Offer:
        price = info.current_cents
        assert price is not None
        return Offer(
            source=self.name, source_id=source_id, title=name or "Nintendo eShop",
            store="Nintendo eShop", price_cents=price,
            regular_cents=info.regular_cents, currency=config.CURRENCY,
            url=f"{LOJA_BR}/{slug}/" if not _NSUID.match(slug) else
            f"https://www.nintendo.com/pt-br/store/products/?nsuid={slug}",
            extra={"sales_status": info.status, "nsuid_br": nsuid or slug,
                   "promo_ate": info.discount_end, "compat": tier,
                   "upgrade_pack": upgrade, "resolved_via": method})


_IMAGE_FIELDS = ("image_url_h16x9_s", "image_url_h2x1_s", "image_url",
                 "image_url_sq_s")


def _image(doc: dict) -> str:
    """Switch 2 titles leave `image_url` empty; their art lives in the
    aspect-ratio fields."""
    for f in _IMAGE_FIELDS:
        v = doc.get(f)
        v = v[0] if isinstance(v, list) and v else v
        if v:
            return f"https:{v}" if str(v).startswith("//") else str(v)
    return ""


provider = Nintendo()
