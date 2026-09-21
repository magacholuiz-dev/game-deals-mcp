"""Product matching corpus.

Titles below were taken from real listings observed while building the
project (Promobit and PS Store, September 2026), so the edge cases are the ones
that actually happen. Each row says what the matcher must decide and why.
"""
import pytest

from game_deals.matching import (
    anchors, match_offer, negative_hits, price_band, within_band)

# (offer title, requested title)
SHOULD_MATCH = [
    # real Nintendo product names carry the trade mark sign
    ("Mario Kart™ World", "Mario Kart World"),
    ("No Man's Sky – Nintendo Switch™ 2 Edition", "No Man's Sky"),
    ("Jogo Grand Theft Auto VI (GTA 6) PS5 - Code in Box", "Grand Theft Auto VI"),
    ("Jogo Grand Theft Auto VI Edição Standard PS5 - Pré Venda", "Grand Theft Auto VI"),
    ("Jogo Grand Theft Auto GTA VI 6, PS5 - TT0002", "Grand Theft Auto VI"),
    ("The Legend Of Zelda Tears Of Kingdom Switch Mídia Física",
     "The Legend of Zelda: Tears of the Kingdom"),
    ("Nintendo Jogo Super Mario Odyssey Nintendo Switch", "Super Mario Odyssey"),
    ("Elden Ring PS5 Midia Fisica", "Elden Ring"),
    ("Jogo Mario Kart 8 Deluxe - Switch", "Mario Kart 8 Deluxe"),
    ("A ascensão do Ronin Edição Digital Deluxe PS5", "Rise of the Ronin"),
    # a negative marker that belongs to the requested title must not block it
    ("Jogo GRIP Combat Racing PS4", "Grip: Combat Racing"),
    ("The Case of the Golden Idol - Switch", "The Case of the Golden Idol"),
    ("Hades - Nintendo Switch Mídia Física", "Hades"),
]

SHOULD_REJECT = [
    # different game in the same series: the numeral is the discriminator
    ("Grand Theft Auto V Premium Edition - GTA 5 PS4", "Grand Theft Auto VI", "anchors"),
    ("Mario Kart World - Nintendo Switch 2", "Mario Kart 8 Deluxe", "anchors"),
    ("Hades II - PC", "Hades", "sequel"),
    # accessories that carry the game's name in the title
    ("Controle Sem Fio DualSense PS5 Edição Limitada Ghost of Yotei",
     "Ghost of Yotei", "negative"),
    ("Boneco Wolverine Marvel Legends 15cm", "Marvel's Wolverine", "negative"),
    ("Amiibo Link Zelda Tears of the Kingdom",
     "The Legend of Zelda: Tears of the Kingdom", "negative"),
    ("Skin Adesiva Console Switch 2 Zelda Tears of the Kingdom",
     "The Legend of Zelda: Tears of the Kingdom", "negative"),
    ("Thumb Grip Analógico Protetor Switch Mario Kart 8 Deluxe",
     "Mario Kart 8 Deluxe", "negative"),
    ("Capa Frontal Faceplate Decorativo PS5 Slim Grand Theft Auto VI",
     "Grand Theft Auto VI", "negative"),
    # console bundles: the game is in the title, the price is 15x
    ("Console PlayStation 5 Slim Edição Digital Marvel's Wolverine",
     "Marvel's Wolverine", "negative"),
    ("Nintendo Switch 32GB 1 Par Joy-con + Mario Kart 8 Deluxe",
     "Mario Kart 8 Deluxe", "negative"),
    # add-ons that are not the base game
    ("Elden Ring: Shadow of the Erdtree DLC PS5", "Elden Ring", "negative"),
    ("Season Pass Mario Kart 8 Deluxe", "Mario Kart 8 Deluxe", "negative"),
]


@pytest.mark.parametrize("offer,query", SHOULD_MATCH)
def test_real_game_listings_match(offer, query):
    r = match_offer(offer, query)
    assert r.ok, f"{offer!r} wrongly rejected: {r.reason}"


@pytest.mark.parametrize("offer,query,why", SHOULD_REJECT)
def test_noise_is_rejected_for_the_right_reason(offer, query, why):
    r = match_offer(offer, query)
    assert not r.ok, f"{offer!r} wrongly accepted"
    keyword = {"anchors": "anchors", "sequel": "sequel",
               "negative": "negative"}[why]
    assert keyword in r.reason, r.reason


def test_corpus_is_large_enough():
    assert len(SHOULD_MATCH) + len(SHOULD_REJECT) >= 10


def test_apostrophes_do_not_split_words():
    """Titles say "Man's", slugs say "mans": both must normalize the same."""
    from game_deals.matching import normalize
    assert normalize("No Man's Sky") == normalize("no-mans-sky") == "no mans sky"
    assert match_offer("no mans sky nintendo switch 2 edition switch 2",
                       "No Man's Sky Switch 2 Edition").ok


def test_numerals_compare_as_numbers():
    a = anchors("Grand Theft Auto VI")
    assert "#6" in a.must
    assert match_offer("GTA 6 PS5 Grand Theft Auto", "Grand Theft Auto VI").ok
    assert not match_offer("Grand Theft Auto V", "Grand Theft Auto VI").ok


def test_negative_is_exempt_only_when_requested():
    assert negative_hits("Jogo Grip Combat Racing", "Grip: Combat Racing") == []
    assert "grip" in negative_hits("Thumb Grip para Switch", "Mario Kart 8")


def test_joycon_spellings_are_all_caught():
    for t in ("Joy-Con", "Joycon", "JOY CON par"):
        assert negative_hits(f"Switch {t} + Jogo", "Zelda"), t


def test_score_prefers_explicit_game_listings():
    plain = match_offer("Grand Theft Auto VI PS5", "Grand Theft Auto VI")
    rich = match_offer("Jogo Grand Theft Auto VI PS5 Mídia Digital",
                       "Grand Theft Auto VI")
    assert rich.score > plain.score


# ------------------------------------------------------------------ price band

def test_symmetric_band_drops_both_faceplate_and_console():
    """Regression: the old cut only removed the expensive side, so a R$ 15,83
    faceplate ended up as 'the price of GTA VI'."""
    prices = [1583, 34417, 35565, 37640, 699900]
    band = price_band(prices)
    kept = [p for p in prices if within_band(p, band)]
    assert kept == [34417, 35565, 37640]


def test_reference_price_gives_a_fixed_fraction_band():
    lo, hi = price_band([1, 2, 3], reference_cents=44990)
    assert lo == int(44990 * 0.35) and hi == int(44990 * 1.25)


def test_too_few_listings_gives_no_band():
    assert price_band([34417, 699900]) is None
    assert within_band(699900, None)     # no band = nothing is cut


def test_console_skus_are_hardware_even_without_the_word_console():
    """Real listings, September 2026. The bundle used to be rejected only by luck
    (it also said "assinatura"); the LCD console had no marker at all."""
    for title in ("Nintendo Switch 2 LCD 256GB Novo",
                  "Bundle Nintendo Switch + Super Mario Bros Wonder",
                  "Console Portátil ROG Xbox Ally X Black 24GB RAM 1TB",
                  "Nintendo Switch OLED Super Mario Bros NSO"):
        assert negative_hits(title, "Super Mario Bros"), title


def test_capacity_marker_is_ignored_when_the_query_asks_for_it():
    assert negative_hits("Cartão SD 256gb", "Cartão SD 256gb") == []


def test_collector_price_filter_uses_explicit_band_then_median():
    from types import SimpleNamespace as S
    from game_deals.collector import filtrar_por_preco
    sinais = [S(price_cents=c) for c in (1583, 34417, 35565, 37640, 699900)]
    # median band, symmetric: faceplate and console both go
    assert [x.price_cents for x in filtrar_por_preco(sinais)] == [34417, 35565, 37640]
    # an explicit band wins, even with too few listings for a median
    two = [S(price_cents=c) for c in (1583, 34417)]
    assert [x.price_cents for x in filtrar_por_preco(two, 44990, 15746)] == [34417]
    assert len(filtrar_por_preco(two)) == 2            # no band possible: keep all


SAME_GAME = [
    ("Mario Kart World", "Mario Kart™ World", True),
    ("Disco Elysium", "Disco Elysium: The Final Cut", True),
    ("Divinity: Original Sin 2", "Divinity: Original Sin 2 – Definitive Edition", True),
    ("The Witcher 3: Wild Hunt", "The Witcher 3: Wild Hunt – Complete Edition", True),
    ("Resident Evil 7: Biohazard", "Resident Evil 7 Biohazard Gold Edition", True),
    ("Legend of Zelda: Breath of the Wild", "The Legend of Zelda™: Breath of the Wild", True),
    ("No Man's Sky Switch 2 Edition", "No Man's Sky – Nintendo Switch™ 2 Edition", True),
    ("Hades", "Hades", True),
    # a different game that merely contains the words: these were wired up wrongly
    ("Resident Evil 2", "Resident Evil Revelations 2", False),
    ("The Legend of Zelda", "The Legend of Zelda: Ocarina of Time", False),
    ("Super Mario Bros.", "Super Mario Bros. Wonder – Nintendo Switch 2 Edition", False),
    ("Hollow Knight", "Hollow Knight: Silksong", False),
    ("Mario Kart 8 Deluxe", "Mario Kart World", False),
    ("Grand Theft Auto V", "Grand Theft Auto V: Premium Edition", True),
]


@pytest.mark.parametrize("asked,found,expected", SAME_GAME)
def test_same_game_accepts_versions_and_rejects_other_games(asked, found, expected):
    from game_deals.matching import same_game
    assert same_game(asked, found) is expected, (asked, found)
