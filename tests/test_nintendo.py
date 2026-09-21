"""Nintendo BR: resolution, classification and pricing, offline, from fixtures
recorded live by scripts/probe.py."""
import json

import pytest

from game_deals import http
from game_deals.providers import nintendo as n
from tests.helpers import fixture_client, load, price_router

LOJA = "https://www.nintendo.com/pt-br/store/products"
PRICE = "https://api.ec.nintendo.com/v1/price"
SITEMAP = "https://www.nintendo.com/pt-br/store/sitemap.xml"

PRICES = ("nintendo/price_br_found.json", "nintendo/price_br_eu_nsuid_not_found.json",
          "nintendo/price_br_eu_nsuid_nms.json", "nintendo/price_br_zelda_switch1.json")

PAGES = {
    "mario-kart-world-switch-2": "nintendo/product_mario_kart_world_switch2.html",
    "no-mans-sky-nintendo-switch-2-edition-switch-2":
        "nintendo/product_no_mans_sky_switch2_edition.html",
    "the-legend-of-zelda-breath-of-the-wild-switch":
        "nintendo/product_zelda_botw_switch1.html",
}


def provider(extra: dict | None = None) -> n.Nintendo:
    routes = {f"{LOJA}/{slug}/": fx for slug, fx in PAGES.items()}
    routes[SITEMAP] = "nintendo/sitemap_pt_br_store.xml"
    routes[PRICE] = price_router(*PRICES)
    routes.update(extra or {})
    http.set_default(fixture_client(routes))
    return n.Nintendo()


# ------------------------------------------------------------ classification

def solr(name):
    return json.loads(load(f"nintendo/{name}"))["response"]["docs"]


def test_solr_native_edition_and_retro_are_told_apart():
    mkw = solr("solr_switch2_native.json")[0]
    assert n.classify(mkw["title"], mkw["system_type"][0]).tier == n.NATIVO

    nms = solr("solr_switch2_edition.json")[0]
    assert "system_type" in nms and "nintendoswitch2" in nms["system_type"][0]
    # Same system_type as a native game: only the title reveals the edition.
    assert n.classify(nms["title"], nms["system_type"][0]).tier == n.EDICAO


def test_upgrade_pack_is_flagged_and_separate_from_the_edition():
    c = n.classify(slug="no-mans-sky-nintendo-switch-2-edition-upgrade-pack-switch-2")
    assert c.tier == n.EDICAO and c.upgrade_pack is True
    plain = n.classify("No Man's Sky – Nintendo Switch™ 2 Edition")
    assert plain.tier == n.EDICAO and plain.upgrade_pack is False


def test_switch1_and_non_switch_systems():
    assert n.classify(slug="hollow-knight-switch").tier == n.RETRO
    old = solr("solr_switch2_native.json")[1]           # Super Mario Kart (SNES)
    assert n.classify(old["title"], old["system_type"][0]).tier is None


def test_search_skips_titles_that_are_not_on_switch():
    p = provider({f"{n.SOLR}": "nintendo/solr_switch2_native.json"})
    titles = [l.title for l in p.search("mario kart world")]
    assert titles == ["Mario Kart World"]        # the two retro consoles dropped


def test_search_never_hands_out_the_european_nsuid_as_the_id():
    p = provider({f"{n.SOLR}": "nintendo/solr_switch2_native.json"})
    hit = p.search("mario kart world")[0]
    assert hit.source_id == "mario-kart-world-switch-2"
    assert hit.extra["nsuid_eu"] == "70010000096802"     # informational only


# --------------------------------------------------------------- resolution

def test_step1_product_page_gives_the_brazilian_nsuid():
    r = provider().resolve(title="Mario Kart World")
    assert r.method == "product_page"
    assert r.nsuid == "70010000095431"             # Americas id, not ...096802
    assert r.tier == n.NATIVO
    assert r.price.regular_cents == 43990


def test_step2_sitemap_finds_the_page_when_the_derived_slug_is_wrong():
    """The derived slug 404s; the sitemap knows the real one."""
    # This wording derives "no-mans-sky-switch-2-edition-switch-2", which does not
    # exist; the real slug has "nintendo-" in it and only the sitemap has it.
    r = provider().resolve(title="No Man's Sky Switch 2 Edition")
    assert r is not None and r.method == "sitemap"
    assert r.slug == "no-mans-sky-nintendo-switch-2-edition-switch-2"
    assert r.nsuid == "70010000099217"


def test_european_nsuid_is_rejected_by_the_brazilian_price_check():
    p = provider()
    assert p.fetch("70010000099138") == []          # EU id of No Man's Sky
    assert "does not price in BR" in p.last_error
    assert p.fetch("70010000096802") == []          # EU id of Mario Kart World


def test_wrong_game_behind_a_similar_slug_is_rejected():
    """Serve the Mario Kart World page where Mario Kart 8 Deluxe is expected:
    the anchors (the numeral 8) must reject it. The page is a recorded live
    fixture, only its URL is misattributed to exercise the validation."""
    p = provider({f"{LOJA}/mario-kart-8-deluxe-switch/":
                  "nintendo/product_mario_kart_world_switch2.html"})
    assert p.resolve(title="Mario Kart 8 Deluxe") is None
    assert "not 'Mario Kart 8 Deluxe'" in p.last_error


def test_edition_request_never_returns_the_switch1_page():
    p = provider({f"{LOJA}/the-legend-of-zelda-breath-of-the-wild-nintendo-switch-2-edition-switch-2/":
                  "nintendo/product_zelda_botw_switch1.html"})
    r = p.resolve(title="The Legend of Zelda: Breath of the Wild – Nintendo Switch 2 Edition")
    assert r is None or r.tier == n.EDICAO


def test_title_not_in_the_brazilian_sitemap_is_not_found():
    assert provider().resolve(title="007 First Light") is None


def test_sitemap_is_parsed_and_contains_the_real_slugs():
    slugs = provider().sitemap_slugs()
    assert "mario-kart-world-switch-2" in slugs
    assert "no-mans-sky-nintendo-switch-2-edition-switch-2" in slugs
    assert not any("007-first-light" in s for s in slugs)


# --------------------------------------------------------------------- price

def test_sale_price_regular_price_and_end_date_come_from_the_price_api():
    """The page JSON-LD shows the promotional price; the API has both and the
    date the promotion ends. NMS Switch 2 Edition was R$ 119,96 until 24/09."""
    page = n.parse_product_page(load("nintendo/product_no_mans_sky_switch2_edition.html").decode())
    assert page.page_price_cents == 11996          # current price, on sale
    p = provider()
    (offer,) = p.fetch("no-mans-sky-nintendo-switch-2-edition-switch-2")
    assert offer.price_cents == 11996
    assert offer.regular_cents == 29990
    assert offer.extra["promo_ate"] == "2026-09-24T06:59:59Z"
    assert offer.extra["compat"] == n.EDICAO
    assert offer.extra["resolved_via"] == "product_page"


def test_full_price_offer_has_no_promo_date():
    (offer,) = provider().fetch("mario-kart-world-switch-2")
    assert offer.price_cents == offer.regular_cents == 43990
    assert offer.extra["promo_ate"] is None


def test_page_parser_reads_all_three_recorded_pages():
    for slug, fx in PAGES.items():
        page = n.parse_product_page(load(fx).decode())
        assert page and len(page.nsuid) == 14 and page.name, slug


def test_robots_refusal_is_reported_not_worked_around():
    robots = "User-agent: *\nDisallow: /pt-br/store/\n"
    p = provider({"https://www.nintendo.com/robots.txt": robots})
    assert p.resolve(title="Mario Kart World") is None
    assert "robots.txt forbids" in p.last_error
