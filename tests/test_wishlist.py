"""wishlist.adiciona: product identity and the Switch 2 existence check."""
from game_deals import db, http, ratings, wishlist
from game_deals.providers import nintendo
from tests.helpers import fixture_client


def ficha(titulo):
    return ratings.Ficha(1, titulo, 90, 4.5, 100, 5000, "2017-03-03", "", ["Nintendo Switch"], 95.0)


def solr_client():
    http.set_default(fixture_client({nintendo.SOLR: "nintendo/solr_switch2_native.json"}))


def test_a_game_that_is_not_on_switch2_is_not_added_as_a_switch2_product(scratch_db):
    """RAWG says "Nintendo Switch"; only the Nintendo catalog knows about Switch 2."""
    solr_client()
    assert wishlist.adiciona("Super Mario Odyssey", "switch2",
                             ficha=ficha("Super Mario Odyssey")) is None
    assert db.list_products() == []


def test_the_same_title_on_two_platforms_gets_two_products(scratch_db, monkeypatch):
    monkeypatch.setattr(wishlist, "_preco", lambda pid, titulo, plat: (None, "sem preço", 0))
    monkeypatch.setattr(wishlist, "_existe_no_switch2", lambda t: True)
    a = wishlist.adiciona("Zelda", "switch", ficha=ficha("The Legend of Zelda: Breath of the Wild"))
    b = wishlist.adiciona("Zelda", "switch2", ficha=ficha("The Legend of Zelda: Breath of the Wild"))
    assert a.product_id != b.product_id
    assert b.product_id.endswith("-switch2")
    assert {p["platform"] for p in db.list_products()} == {"switch", "switch2"}


def test_adding_the_same_product_again_keeps_one_nintendo_alias(scratch_db):
    db.upsert_product("z", "Zelda", "game", "switch")
    db.add_alias("z", "nintendo", "old-slug")
    db.replace_alias("z", "nintendo", "new-slug")
    assert [a["source_id"] for a in db.aliases_for("z")] == ["new-slug"]
