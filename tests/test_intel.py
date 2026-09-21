"""Aggregate verdict, freshness, typical discount depth and buy/wait."""
import datetime as dt

import pytest

from game_deals import db, intel, scheduler as sch

D = 86400


def ts(date: dt.date, hour: int = 12) -> int:
    return int(dt.datetime(date.year, date.month, date.day, hour).timestamp())


NOW = ts(dt.date(2026, 11, 5))                # Black Friday is 22 days away
QUIET = ts(dt.date(2026, 8, 3))               # nothing on the calendar


@pytest.fixture(autouse=True)
def _db(scratch_db):
    pass


def product(pid, title="Jogo", platform="switch2", publisher="", released=""):
    db.upsert_product(pid, title, "game", platform)
    if publisher:
        db.set_publisher(pid, publisher)
    if released:
        db.conn().execute("UPDATE products SET released=? WHERE id=?", (released, pid))
        db.conn().commit()


def seed(pid, store, price, days_ago, now=NOW, regular=None, source="nintendo",
         in_stock=True):
    db.record(pid, source, store, price, regular, "BRL", in_stock, f"https://x/{store}",
              ts=now - int(days_ago * D))


def history(pid, store, prices_by_days_ago, now=NOW, regular=None, source="nintendo"):
    for days, price in prices_by_days_ago:
        seed(pid, store, price, days, now, regular, source)


# =============================================================== aggregate

def test_best_is_the_lowest_fresh_in_stock_price_across_stores():
    product("p")
    seed("p", "Loja A", 30000, 0.1)
    seed("p", "Loja B", 25000, 0.2)
    seed("p", "Loja C", 27000, 0.1)
    a = intel.veredito_agregado("p", NOW)
    assert a.best.store == "Loja B" and a.best.price_cents == 25000
    assert a.spread_cents == 2000                       # 27000 - 25000
    assert [q.store for q in a.fresh] == ["Loja B", "Loja C", "Loja A"]


def test_stale_listing_is_not_presented_as_an_active_deal():
    """A store abandoned six months ago, with a low price, must not win."""
    product("p")
    seed("p", "Loja Abandonada", 15000, 180)
    for d in (60, 40, 20, 5, 0.1):
        seed("p", "Loja Viva", 20000, d)
    a = intel.veredito_agregado("p", NOW)
    assert a.best.store == "Loja Viva"
    assert [q.store for q in a.stale] == ["Loja Abandonada"]
    assert "preço antigo ignorado" in a.freshness_note and "Loja Abandonada" in a.freshness_note


def test_but_the_stale_price_still_counts_as_history():
    """Freshness filters what is buyable now, not what was ever true."""
    product("p")
    seed("p", "Loja Abandonada", 15000, 180)
    for d in (100, 60, 40, 20, 5, 0.1):
        seed("p", "Loja Viva", 20000, d)
    a = intel.veredito_agregado("p", NOW)
    assert not a.is_all_time_low                       # 15000 was cheaper, once
    assert "todas as lojas" in a.label and "meses" in a.label


def test_out_of_stock_is_not_buyable():
    product("p")
    seed("p", "Barata", 10000, 0.1, in_stock=False)
    seed("p", "Cara", 20000, 0.1)
    assert intel.veredito_agregado("p", NOW).best.store == "Cara"


def test_nothing_fresh_gives_no_current_price():
    product("p")
    seed("p", "Antiga", 10000, 90)
    a = intel.veredito_agregado("p", NOW)
    assert a.best is None and "sem preço atual" in a.label


def test_all_stores_out_of_stock_is_reported_as_such():
    product("p")
    seed("p", "L", 10000, 0.1, in_stock=False)
    assert "nenhuma loja com estoque" in intel.veredito_agregado("p", NOW).label


def test_all_time_low_across_stores_uses_the_across_stores_phrase():
    product("p")
    history("p", "Loja A", [(d, 30000) for d in range(90, 0, -6)])
    history("p", "Loja B", [(d, 29000) for d in range(89, 1, -6)])
    seed("p", "Loja B", 20000, 0.1)
    a = intel.veredito_agregado("p", NOW)
    assert a.is_all_time_low and "menor preço já registrado em todas as lojas" in a.label


def test_freshness_follows_each_sources_own_cadence(monkeypatch):
    """A weekly source is fresh at 15 days; a daily one is stale at 4."""
    monkeypatch.setitem(sch.CADENCE_S, "itad", 7 * D)
    monkeypatch.setitem(sch.CADENCE_S, "steam", D)
    assert intel.fresh_limit_s("itad") == 21 * D
    assert intel.fresh_limit_s("steam") == 3 * D                      # the floor
    product("p")
    seed("p", "Semanal", 20000, 15, source="itad")
    seed("p", "Diaria", 19000, 4, source="steam")
    a = intel.veredito_agregado("p", NOW)
    assert a.best.store == "Semanal"
    assert [q.store for q in a.stale] == ["Diaria"]


def test_community_feed_rows_never_count_as_a_store():
    product("p")
    seed("p", "Comunidade", 5000, 0.1, source="promobit")     # a feed, not a store
    seed("p", "Loja", 20000, 0.1)
    assert intel.veredito_agregado("p", NOW).best.store == "Loja"


# ====================================================== typical discount depth

def test_publisher_basis_wins_when_it_has_enough_products():
    for i, depth in enumerate((0.50, 0.60, 0.40, 0.55)):
        product(f"n{i}", platform="switch2", publisher="Nintendo")
        seed(f"n{i}", "L", int(10000 * (1 - depth)), 3, regular=10000)
    product("o", platform="switch2", publisher="Outra")
    seed("o", "L", 9000, 3, regular=10000)
    t = intel.typical_discount_depth("switch2", "Nintendo")
    assert t["base"] == "publisher" and t["produtos"] == 4 and t["pct"] == 52  # median 0.525


def test_platform_basis_is_the_fallback_and_says_so():
    for i, depth in enumerate((0.30, 0.20, 0.40)):
        product(f"s{i}", platform="ps5", publisher="")
        seed(f"s{i}", "L", int(10000 * (1 - depth)), 3, regular=10000)
    t = intel.typical_discount_depth("ps5", "Publisher Novo")
    assert (t["base"], t["produtos"], t["pct"]) == ("plataforma", 3, 30)


def test_no_basis_when_there_are_too_few_products():
    product("a", platform="ps5")
    seed("a", "L", 5000, 3, regular=10000)
    assert intel.typical_discount_depth("ps5", "")["pct"] is None
    assert intel.typical_discount_depth("ps5", "")["base"] == "nenhuma"


def test_one_long_sale_counts_once_not_once_per_reading():
    """Fifty readings of the same 30% sale must not outweigh three products."""
    product("long", platform="ps5")
    for i in range(50):
        seed("long", "L", 7000, i / 10, regular=10000)
    for i in range(2):
        product(f"x{i}", platform="ps5")
        seed(f"x{i}", "L", 4000, 3, regular=10000)           # 60% off
    t = intel.typical_discount_depth("ps5", "")
    assert t["produtos"] == 3 and t["pct"] == 60             # median of 30, 60, 60


# ================================================================ buy or wait

def comparables(platform="switch2", depths=(0.50, 0.55, 0.60), now=NOW):
    for i, d in enumerate(depths):
        product(f"c{i}", platform=platform)
        seed(f"c{i}", "L", int(10000 * (1 - d)), 3, now, regular=10000)


def test_unreleased_game_is_neutral_and_explains_pre_orders():
    product("gta", "GTA VI", "ps5", released="2026-11-19")
    seed("gta", "PS Store", 44990, 0.1)
    r = intel.recomendar_compra("gta", dt.date(2026, 11, 5), NOW)
    assert r.decisao == intel.NEUTRO and "pré-venda não entra em promoção" in r.justificativa[0]


def test_no_current_price_is_neutral_with_maximum_uncertainty():
    product("p")
    r = intel.recomendar_compra("p", dt.date(2026, 11, 5), NOW)
    assert r.decisao == intel.NEUTRO and r.incerteza == 1.0 and r.incerteza_texto == "alta"


def test_all_time_low_with_history_is_buy_now():
    product("p")
    history("p", "L", [(d, 30000) for d in range(80, 1, -4)])
    seed("p", "L", 22000, 0.1)
    r = intel.recomendar_compra("p", dt.date(2026, 11, 5), NOW)
    assert r.decisao == intel.COMPRE_AGORA
    assert "menor preço já registrado" in r.justificativa[0]


def test_major_sale_close_and_shallow_discount_says_wait():
    comparables()                                            # typical depth 55%
    product("p")
    history("p", "L", [(d, 30000) for d in (80, 60, 40, 30, 20)])
    seed("p", "L", 27000, 0.1, regular=30000)                # 10% off
    seed("p", "L", 26000, 0.05, regular=30000)               # but it was cheaper... 
    r = intel.recomendar_compra("p", dt.date(2026, 11, 5), NOW)
    assert r.decisao == intel.ESPERE
    text = " ".join(r.justificativa)
    assert "Black Friday 2026 começa em 22 dias" in text and "raso" in text
    assert r.fatores["evento"]["certeza"] == "rule"
    assert r.fatores["desconto_tipico"] == {"pct": 55, "base": "plataforma", "produtos": 3}


def test_estimated_event_raises_the_uncertainty():
    """Same situation, but the event is only an estimate: trust it less."""
    now = ts(dt.date(2027, 2, 20))
    comparables(now=now)
    product("p")
    history("p", "L", [(d, 30000) for d in (80, 60, 40, 30, 20)], now=now)
    seed("p", "L", 27000, 0.1, now, regular=30000)
    est = intel.recomendar_compra("p", dt.date(2027, 2, 20), now)
    assert est.decisao == intel.ESPERE and est.fatores["evento"]["certeza"] == "estimated"

    known = _black_friday_case()
    assert est.incerteza > known.incerteza


def _black_friday_case():
    db.conn().executescript("DELETE FROM price_points; DELETE FROM products;")
    comparables()
    product("p")
    history("p", "L", [(d, 30000) for d in (80, 60, 40, 30, 20)])
    seed("p", "L", 27000, 0.1, regular=30000)
    return intel.recomendar_compra("p", dt.date(2026, 11, 5), NOW)


def test_a_sale_that_is_already_running_does_not_say_wait():
    now = ts(dt.date(2026, 11, 28))                # Black Friday itself
    comparables(now=now)
    product("p")
    history("p", "L", [(d, 30000) for d in (80, 60, 40, 30, 20)], now=now)
    seed("p", "L", 27000, 0.1, now, regular=30000)
    r = intel.recomendar_compra("p", dt.date(2026, 11, 28), now)
    assert "evento" not in r.fatores


def test_steam_sales_are_ignored_for_console_products_and_minor_sales_do_not_count():
    # Steam Autumn (minor) is 6 days away: it never triggers "wait", not even for pc.
    now = ts(dt.date(2026, 9, 25))
    product("pc", platform="pc")
    history("pc", "L", [(d, 30000) for d in (80, 60, 40, 30, 20)], now=now)
    seed("pc", "L", 27000, 0.1, now, regular=30000)
    assert "evento" not in intel.recomendar_compra("pc", dt.date(2026, 9, 25), now).fatores

    # Steam Winter (major, pc only) is 16 days away, after Black Friday ended.
    now = ts(dt.date(2026, 12, 1))
    comparables(platform="switch2", now=now)
    product("sw", platform="switch2")
    history("sw", "L", [(d, 30000) for d in (80, 60, 40, 30, 20)], now=now)
    seed("sw", "L", 27000, 0.1, now, regular=30000)
    assert "evento" not in intel.recomendar_compra("sw", dt.date(2026, 12, 1), now).fatores
    product("pc2", platform="pc")
    history("pc2", "L", [(d, 30000) for d in (80, 60, 40, 30, 20)], now=now)
    seed("pc2", "L", 27000, 0.1, now, regular=30000)
    r = intel.recomendar_compra("pc2", dt.date(2026, 12, 1), now)
    assert r.fatores["evento"]["nome"] == "Steam Winter Sale 2026"


def test_shallow_is_relative_to_the_typical_depth_when_known():
    comparables(depths=(0.60, 0.60, 0.60))         # typical 60% => shallow < 30%
    product("p")
    history("p", "L", [(d, 30000) for d in (80, 60, 40, 30, 20)])
    seed("p", "L", 24000, 0.1, regular=30000)      # 20% off
    assert intel.recomendar_compra("p", dt.date(2026, 11, 5), NOW).decisao == intel.ESPERE
    seed("p", "L", 18000, 0.05, regular=30000)     # 40% off: not shallow
    r = intel.recomendar_compra("p", dt.date(2026, 11, 5), NOW)
    assert "evento" not in r.fatores


def test_discount_matching_the_typical_depth_is_buy_now():
    comparables(depths=(0.30, 0.30, 0.30))
    product("p")
    history("p", "L", [(d, 30000) for d in (80, 60, 40, 30, 20)])
    seed("p", "L", 20000, 0.1, regular=30000)      # 33% off vs typical price
    seed("p", "L", 15000, 200)                     # ...but was cheaper 200 days ago
    r = intel.recomendar_compra("p", QUIET_DATE, QUIET)
    assert r.decisao == intel.COMPRE_AGORA
    assert "iguala ou supera o típico de 30%" in r.justificativa[0]


QUIET_DATE = dt.date(2026, 8, 3)


def test_not_a_promotion_when_it_was_cheaper_recently_says_wait():
    product("p")
    history("p", "L", [(d, 20000) for d in (80, 60, 40, 30)])
    seed("p", "L", 30000, 0.1)
    r = intel.recomendar_compra("p", QUIET_DATE, NOW)
    assert r.decisao == intel.ESPERE and "não é promoção" in r.justificativa[0]


def test_nothing_special_is_neutral():
    product("p")
    history("p", "L", [(d, 25000) for d in (100, 80, 60, 40, 20, 5)])
    seed("p", "L", 25000, 0.1)
    assert intel.recomendar_compra("p", QUIET_DATE, NOW).decisao == intel.NEUTRO


def test_uncertainty_is_higher_with_thin_history_and_no_typical_depth():
    product("thin")
    seed("thin", "L", 20000, 0.1)
    seed("thin", "L", 19000, 0.05)
    thin = intel.recomendar_compra("thin", QUIET_DATE, NOW)
    comparables(depths=(0.3, 0.3, 0.3))
    product("rich")
    history("rich", "L", [(d, 30000) for d in range(150, 0, -5)])
    seed("rich", "L", 20000, 0.1, regular=30000)
    rich = intel.recomendar_compra("rich", QUIET_DATE, NOW)
    assert thin.incerteza > rich.incerteza
    assert 0.0 <= rich.incerteza <= 1.0 and rich.incerteza_texto in ("baixa", "média", "alta")


def test_unknown_product_is_an_error_not_a_recommendation():
    with pytest.raises(KeyError):
        intel.recomendar_compra("nope", QUIET_DATE, NOW)


def test_a_product_is_never_part_of_its_own_typical_depth():
    for i, d in enumerate((0.5, 0.5, 0.5)):
        product(f"c{i}", platform="ps5")
        seed(f"c{i}", "L", 5000, 3, regular=10000)
    product("me", platform="ps5")
    seed("me", "L", 9000, 3, regular=10000)                 # a shallow 10%
    with_me = intel.typical_discount_depth("ps5", "")
    without = intel.typical_discount_depth("ps5", "", exclude_product="me")
    assert with_me["produtos"] == 4 and without["produtos"] == 3
    assert without["pct"] == 50
