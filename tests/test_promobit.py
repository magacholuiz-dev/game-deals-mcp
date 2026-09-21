"""Promobit: only the public listing is read, and only what robots.txt allows.

The fixtures include the REAL robots.txt of both hosts, so the compliance
tests are checked against what the sites actually publish."""
import pytest

from game_deals import http
from game_deals.providers import promobit as pb
from tests.helpers import fixture_client, load

WWW = "https://www.promobit.com.br"
API = "https://api.promobit.com.br"


def provider() -> tuple[pb.Promobit, http.HttpClient]:
    client = fixture_client({
        f"{WWW}/promocoes/games/": "promobit/listing_promocoes_games.html",
        f"{WWW}/robots.txt": "promobit/robots_www.txt",
        f"{API}/robots.txt": "promobit/robots_api.txt",
        # A trap: if the provider ever regressed to the closed API, this would
        # answer 200 and the tests below would notice the request in `calls`.
        f"{API}/search": {"active_offers": [{"offer_title": "must not be read"}]},
    })
    http.set_default(client)
    return pb.Promobit(), client


# ---------------------------------------------------------------- compliance

def test_api_host_is_closed_by_its_own_robots_txt():
    _, client = provider()
    with pytest.raises(http.RobotsBlocked):
        client.get(f"{API}/search", params={"q": "zelda"})
    assert not any("/search" in u for u in client.calls)     # never even sent


def test_provider_never_contacts_the_api_host():
    p, client = provider()
    p.sinais_do_titulo("The Last of Us Part II")
    p.sinais("zelda")
    assert not any(u.startswith(API) and "robots.txt" not in u for u in client.calls)


def test_listing_is_allowed_and_search_is_not():
    _, client = provider()
    assert client.allowed(f"{WWW}/promocoes/games/")
    assert not client.allowed(f"{WWW}/buscar/zelda")        # wildcard /buscar*
    assert not client.allowed(f"{WWW}/api/anything")


def test_robots_refusal_is_reported_and_yields_nothing():
    http.set_default(fixture_client({
        f"{WWW}/robots.txt": "User-agent: *\nDisallow: /promocoes/\n",
        f"{WWW}/promocoes/games/": "promobit/listing_promocoes_games.html"}))
    p = pb.Promobit()
    assert p.listing() == []
    assert "robots.txt forbids" in p.last_error


# ------------------------------------------------------------------- parsing

def test_listing_fixture_parses_all_twelve_offers():
    sinais = pb.parse_listing(load("promobit/listing_promocoes_games.html").decode())
    assert len(sinais) == 12
    console = sinais[0]
    assert console.price_cents == 772708 and console.old_price_cents == 913871
    assert console.loja == "Shopee" and console.publicado_ts > 0
    assert console.url.startswith(f"{WWW}/oferta/")


def test_unexpected_page_yields_no_offers_instead_of_crashing():
    assert pb.parse_listing("<html>no state here</html>") == []
    assert pb.parse_listing('<script id="__NEXT_DATA__">{"props":{}}</script>') == []


# ------------------------------------------------------------------ matching

def test_finds_a_real_game_deal_on_the_listing():
    p, _ = provider()
    (s,) = p.sinais_do_titulo("The Last of Us Part II")
    assert s.loja == "Netshoes" and s.price_cents == 8001
    assert "PS4" in s.titulo


def test_hardware_and_gift_cards_never_match_a_game_query():
    p, _ = provider()
    # ("Razer Gold" is not here on purpose: that query names the gift card
    # itself, so matching it is correct.)
    for game in ("Playstation Store", "Nintendo Switch OLED", "Super Mario Bros"):
        assert p.sinais_do_titulo(game) == [], game


def test_accessory_can_still_be_tracked_when_the_query_asks_for_it():
    """The negative markers ignore words present in the query, so a tracked
    accessory ("Controle DualSense") is found while games never match it."""
    p, _ = provider()
    hits = p.sinais_do_titulo("Controle DualSense PS5")
    assert [h.loja for h in hits] == ["Netshoes"] and hits[0].price_cents == 37350


def test_platform_filter_uses_the_listing_subcategory():
    p, _ = provider()
    assert p.sinais_do_titulo("The Last of Us Part II", plataforma="ps4")
    assert p.sinais_do_titulo("The Last of Us Part II", plataforma="switch") == []


def test_feed_is_never_a_price_source():
    assert pb.Promobit.kind == "feed"
    assert pb.Promobit().fetch("123") == []
