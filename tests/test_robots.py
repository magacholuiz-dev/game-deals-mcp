from game_deals.robots import Robots

UA = "game-deals-mcp/0.2 (+https://github.com/x/y; personal use)"

# Real robots.txt of api.promobit.com.br as fetched on 2026-09-21 (trimmed
# comments): everything is closed except the link-preview redirect route.
PROMOBIT_API = """
User-agent: Googlebot
Disallow:

User-Agent: *
Allow: /v4/redirect/
Disallow: /
"""

# Excerpt of www.promobit.com.br/robots.txt, wildcard rules included.
PROMOBIT_WWW = """
User-agent: *
Allow: *.css
Allow: *.js
Disallow: /buscar*
Disallow: /api/*
Disallow: /oferta/check/*
Disallow: /v2*
"""


def test_promobit_api_is_closed_to_us():
    r = Robots.parse(PROMOBIT_API)
    assert not r.allowed(UA, "/search")
    assert not r.allowed(UA, "/offers")
    assert r.allowed(UA, "/v4/redirect/abc")          # longest match: Allow


def test_named_group_does_not_leak_to_other_agents():
    r = Robots.parse(PROMOBIT_API)
    assert r.allowed("Googlebot", "/search")           # Disallow: (empty)
    assert not r.allowed(UA, "/search")


def test_wildcard_disallow_is_enforced():
    """robotparser would treat '/buscar*' as a literal path and allow this."""
    r = Robots.parse(PROMOBIT_WWW)
    assert not r.allowed(UA, "/buscar/zelda")
    assert not r.allowed(UA, "/buscar?q=zelda")
    assert not r.allowed(UA, "/api/anything")
    assert r.allowed(UA, "/promocoes/games/")
    assert r.allowed(UA, "/oferta/nintendo-switch-2-lcd-256gb-novo-3010372")
    assert not r.allowed(UA, "/oferta/check/123")


def test_dollar_anchor_and_tie_goes_to_allow():
    r = Robots.parse("User-agent: *\nDisallow: /*.pdf$\nAllow: /public.pdf$\n")
    assert not r.allowed(UA, "/a/b.pdf")
    assert r.allowed(UA, "/public.pdf")
    assert r.allowed(UA, "/a/b.pdf.html")              # $ anchors the end


def test_missing_robots_allows_and_server_error_denies():
    assert Robots.unavailable(404).allowed(UA, "/x")
    assert Robots.unavailable(403).allowed(UA, "/x")
    assert not Robots.unavailable(503).allowed(UA, "/x")
    assert not Robots.unavailable(None).allowed(UA, "/x")


def test_no_group_for_agent_means_open():
    r = Robots.parse("User-agent: SomeoneElse\nDisallow: /\n")
    assert r.allowed(UA, "/anything")
