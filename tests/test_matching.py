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
