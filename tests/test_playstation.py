"""PlayStation Store BR from live fixtures: editions, sales, and the guarantee
that one edition's price is never reported as another's."""
import pytest

from game_deals import http
from game_deals.providers import playstation as ps
from tests.helpers import fixture_client, load

BASE = "https://store.playstation.com/pt-br"
GTA6_STD = "EP1004-PPSA01547_00-GTAVISTANDARD001"
GTA6_ULT = "EP1004-PPSA01547_00-GTAVIULTIMATE001"


def provider(extra: dict | None = None) -> ps.PlayStation:
    routes = {
        f"{BASE}/concept/10000730": "playstation/concept_gta6_editions.html",
        f"{BASE}/concept/10003386": "playstation/concept_ronin_on_sale.html",
        f"{BASE}/product/{GTA6_STD}": "playstation/product_gta6_standard.html",
        # The Standard product page embeds the whole concept, Ultimate included
        # (checked in test_product_page_embeds_every_edition), so it also serves
        # this URL. Only the URL is attributed, the content is a live recording.
        f"{BASE}/product/{GTA6_ULT}": "playstation/product_gta6_standard.html",
        f"{BASE}/category/3f772501-f6f8-49b7-abac-874a88ca4897/1":
            "playstation/category_deals_shell.html",
        "https://store.playstation.com/robots.txt": "playstation/robots.txt",
    }
    routes.update(extra or {})
    http.set_default(fixture_client(routes))
    return ps.PlayStation()


def page(name: str) -> ps.PsPage:
    return ps.parse_page(load(f"playstation/{name}").decode())


# -------------------------------------------------------------- extraction

def test_concept_lists_every_edition_with_its_own_price():
    pg = page("concept_gta6_editions.html")
    by = {e.tier: e for e in pg.sellable()}
    assert set(by) == {"standard", "ultimate"}
    assert by["standard"].price_cents == 44990
    assert by["ultimate"].price_cents == 54990
    assert all(not e.on_sale for e in by.values())          # pre-order, no discount


def test_product_page_embeds_every_edition():
    """Why the Ultimate route above may serve the Standard page's content."""
    tiers = {e.tier for e in page("product_gta6_standard.html").sellable()}
    assert tiers == {"standard", "ultimate"}


def test_upgrade_pack_is_not_a_sellable_edition():
    pg = page("concept_gta6_editions.html")
    assert "upgrade" in {e.tier for e in pg.editions}
    assert "upgrade" not in {e.tier for e in pg.sellable()}


def test_sale_price_regular_price_and_end_timestamp():
    ronin = {e.tier: e for e in page("concept_ronin_on_sale.html").sellable()}
    deluxe = ronin["deluxe"]
    assert (deluxe.price_cents, deluxe.regular_cents) == (30089, 45590)
    assert deluxe.on_sale
    assert deluxe.sale_end_ts == 1790233140                 # ms in the page / 1000
    assert deluxe.sale_end_iso == "2026-09-24T06:59:00Z"


def test_ps_plus_inclusion_is_not_mistaken_for_a_free_game():
    """The catalog CTA says "Incluído" with a discounted value of 0. The real
    price of the base game is R$ 399,90, and PS Plus is only flagged."""
    std = {e.tier: e for e in page("concept_ronin_on_sale.html").sellable()}["standard"]
    assert std.price_cents == 39990 and std.ps_plus_included is True
    assert not std.free and not std.on_sale


def test_demo_is_never_a_sellable_edition():
    tiers = {e.tier for e in page("concept_ronin_on_sale.html").sellable()}
    assert "demo" not in tiers


# --------------------------------------------- the edition regression check

def test_asking_for_ultimate_returns_the_ultimate_price_not_standard():
    (o,) = provider().fetch("10000730#ultimate")
    assert o.price_cents == 54990 and o.extra["edition"] == "ultimate"


def test_asking_for_standard_returns_standard():
    (o,) = provider().fetch("10000730#standard")
    assert o.price_cents == 44990


def test_missing_edition_returns_nothing_instead_of_falling_back():
    """GTA VI has no Deluxe. A silent fallback would record the Standard price
    (R$ 449,90) under a Deluxe product."""
    p = provider()
    assert p.fetch("10000730#deluxe") == []
    assert "no 'deluxe' edition" in p.last_error
    assert "standard, ultimate" in p.last_error          # says what exists


def test_bare_concept_with_several_editions_is_refused_as_ambiguous():
    p = provider()
    assert p.fetch("10000730") == []
    assert "several editions" in p.last_error


def test_bare_concept_with_two_editions_on_sale_page_is_refused_too():
    p = provider()
    assert p.fetch("concept/10003386") == []              # standard + deluxe


def test_explicit_product_id_is_unambiguous():
    (std,) = provider().fetch(f"product/{GTA6_STD}")
    (ult,) = provider().fetch(f"product/{GTA6_ULT}")
    assert (std.price_cents, ult.price_cents) == (44990, 54990)


def test_deluxe_on_sale_carries_the_promotion_end():
    (o,) = provider().fetch("10003386#deluxe")
    assert (o.price_cents, o.regular_cents) == (30089, 45590)
    assert o.extra["promo_ate"] == "2026-09-24T06:59:00Z"
    assert o.extra["editions_on_page"] == ["standard", "deluxe"]


def test_standard_next_to_a_discounted_deluxe_has_no_sale():
    (o,) = provider().fetch("10003386#standard")
    assert o.price_cents == 39990 and o.regular_cents is None
    assert o.extra["promo_ate"] is None


# ---------------------------------------------------------------- discovery

def test_category_pages_are_a_client_rendered_shell():
    """Verified live on three category ids: no products in the HTML. The
    provider must say so instead of returning an empty list silently."""
    p = provider()
    assert p.discover_category("3f772501-f6f8-49b7-abac-874a88ca4897", 1) == []
    assert "client-side rendered" in p.last_error


def test_category_shell_fixture_really_has_no_products():
    html = load("playstation/category_deals_shell.html").decode()
    assert ps.parse_category_shell(html) == []
    assert "basePrice" not in html


def test_search_is_documented_as_unavailable():
    assert provider().search("gta") == []


# ------------------------------------------------------------------ robots

def test_recorded_robots_txt_allows_our_pages():
    p = provider()
    assert p.fetch("10000730#standard")            # would be [] if blocked


def test_robots_refusal_is_reported():
    p = provider({"https://store.playstation.com/robots.txt":
                  "User-agent: *\nDisallow: /pt-br/\n"})
    assert p.fetch("10000730#standard") == []
    assert "robots.txt forbids" in p.last_error


def test_unknown_page_reports_its_status():
    p = provider()
    assert p.fetch("99999999#standard") == []
    assert "404" in p.last_error
