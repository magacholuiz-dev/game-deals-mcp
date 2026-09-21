"""Deciding whether a store listing IS the game we asked about.

Store titles are noisy. Searching "Grand Theft Auto VI" returns the game, GTA V,
a faceplate, a console bundle and a gift card, and no field in any source tells
them apart, so this module decides from the title text and the price.

Three layers, cheapest first, each independently tested:

1. anchors:   distinctive tokens of the requested title must appear (numerals
              compare as numbers, so "VI" matches "6" but never "V").
2. negatives: words that mark accessories, gift cards, currency packs, DLC and
              console bundles. Matching is by whole token, and a negative is
              ignored when the requested title itself contains it, otherwise
              the game "Control" could never match anything.
3. price band: a listing far from the plausible range is a bundle or an
              accessory whatever its title says (see price_band).
"""
from __future__ import annotations

import re
import statistics
import unicodedata
from dataclasses import dataclass

# Words that carry no identity. Kept short on purpose: removing too much turns
# the anchors into something every listing satisfies.
NOISE = {"de", "da", "do", "the", "of", "a", "o", "e", "and", "-", "edition",
         "edicao", "deluxe", "hd", "remaster", "remastered", "definitive",
         "standard", "ultimate", "jogo", "game", "ps4", "ps5", "switch",
         "nintendo", "playstation", "xbox", "pc", "midia", "fisica", "digital"}

ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7",
         "viii": "8", "ix": "9", "x": "10"}

# --- negative anchors --------------------------------------------------------
# Single tokens are matched as whole words; entries with a space are phrases.
NEGATIVE_TOKENS = {
    # controllers and add-ons
    "controle", "controller", "gamepad", "joystick", "joy", "joycon", "grip",
    "thumbstick", "thumbsticks", "analogico", "analogicos", "volante", "gatilho",
    # protection and looks
    "capa", "capinha", "case", "estojo", "bolsa", "mochila", "pelicula",
    "protetor", "skin", "skins", "adesivo", "adesivos", "faceplate", "placa",
    # merchandise
    "amiibo", "figure", "figura", "boneco", "boneca", "funko", "camiseta",
    "caneca", "chaveiro", "poster", "quadro", "pelucia", "miniatura", "llavero",
    # peripherals and hardware
    "headset", "fone", "suporte", "carregador", "cabo", "dock", "bateria",
    "teclado", "mouse", "monitor", "cooler", "ssd", "hd", "pendrive",
    "console", "videogame", "notebook",
    # console SKUs: a listing that names a screen type or a bundle is hardware
    "bundle", "oled", "lite", "lcd", "slim",
    # money, subscriptions, in-game currency
    "gift", "voucher", "credito", "creditos", "assinatura", "moedas", "moeda",
    "vbucks", "robux", "coins", "pontos", "points", "cash", "gold",
    # add-ons that are not the base game
    "dlc", "expansao", "upgrade", "melhoria", "atualizacao", "trilha", "soundtrack",
}
NEGATIVE_PHRASES = (
    "cartao presente", "cartao de memoria", "memory card", "micro sd",
    "cartao pre pago", "passe de temporada", "season pass", "game pass",
    "ps plus", "playstation plus", "nintendo switch online", "hd externo",
    "video game", "action figure", "kit de",
)

_CAPACITY = re.compile(r"\d+(?:gb|tb)")

POSITIVE_PHRASES = ("midia fisica", "midia digital", "edicao standard",
                    "standard edition", "code in box", "codigo digital",
                    "edicao deluxe", "edicao ultimate")


# "Nintendo Switch 2 Edition" says WHICH VERSION of a game a listing is, not WHICH
# GAME it is. Left in, the "2" became a required numeral and the trailing words
# pushed the real name out of the anchors. Version is checked separately, by
# the classifier in providers/nintendo.py.
_EDITION_MARKERS = re.compile(
    r"\b(?:nintendo\s+)?switch\s*2\s+edition\b|\bedicao\s+(?:para\s+)?(?:o\s+)?switch\s*2\b"
    r"|\bupgrade\s+pack\b|\bpacote\s+de\s+(?:upgrade|melhoria)\b")


def normalize(text: str) -> str:
    """Lowercase, strip accents, turn punctuation into single spaces."""
    # Drop trademark symbols BEFORE NFKD: compatibility decomposition turns the
    # trade mark sign into the letters "TM", so "Mario Kart™ World" became
    # "mario karttm world" and never matched. Found with real Nintendo pages.
    text = text.replace("™", "").replace("®", "").replace("©", "")
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    t = t.replace("&", " and ")
    # An apostrophe belongs to the word: "Man's" -> "mans", the same form the
    # Nintendo slug uses ("no-mans-sky"). Turning it into a space split the word
    # into "man" and "s" and the two sides never agreed.
    t = re.sub(r"[\u0027\u2019\u2018`]", "", t)
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def tokens(text: str) -> list[str]:
    return normalize(text).split()


# --------------------------------------------------------------------- anchors

@dataclass(frozen=True)
class Anchors:
    must: tuple[str, ...]      # every one has to appear ("#6" = the number 6)
    should: tuple[str, ...]    # at least half have to appear when there are 2+

    def __bool__(self) -> bool:
        return bool(self.must or self.should)


def anchors(title: str) -> Anchors:
    stripped = _EDITION_MARKERS.sub(" ", normalize(title))
    words = [w for w in stripped.split() if w not in NOISE]
    if not words:
        return Anchors((), ())
    must: list[str] = []
    should: list[str] = []
    name = words[-1]
    for w in words:
        num = ROMAN.get(w) or (w if w.isdigit() and len(w) <= 2 else None)
        if num:
            must.append(f"#{num}")
        elif w == name:
            must.append(w)
        else:
            should.append(w)
    return Anchors(tuple(dict.fromkeys(must)), tuple(dict.fromkeys(should)))


def _has_number(offer_tokens: set[str], num: str) -> bool:
    roman = next((r for r, n in ROMAN.items() if n == num), "")
    return num in offer_tokens or (bool(roman) and roman in offer_tokens)


def has_anchors(offer_title: str, a: Anchors) -> bool:
    if not a:
        return False
    toks = set(tokens(offer_title))
    flat = " " + " ".join(tokens(offer_title)) + " "
    for m in a.must:
        if m.startswith("#"):
            if not _has_number(toks, m[1:]):
                return False
        elif m not in toks and f" {m} " not in flat:
            return False
    if len(a.should) >= 2:
        hits = sum(1 for s in a.should if s in toks)
        if hits < (len(a.should) + 1) // 2:
            return False
    return True


# ------------------------------------------------------------------- negatives

def negative_hits(offer_title: str, query_title: str = "") -> list[str]:
    """Negative markers present in the offer and NOT part of what was asked."""
    offer_norm = normalize(offer_title)
    offer_toks = set(offer_norm.split())
    query_norm = normalize(query_title)
    query_toks = set(query_norm.split())
    hits = [t for t in sorted(NEGATIVE_TOKENS & offer_toks) if t not in query_toks]
    # Storage sizes ("256gb", "1tb") only appear on consoles, drives and cards.
    hits += [t for t in sorted(offer_toks)
             if _CAPACITY.fullmatch(t) and t not in query_toks]
    for phrase in NEGATIVE_PHRASES:
        if f" {phrase} " in f" {offer_norm} " and phrase not in query_norm:
            hits.append(phrase)
    # "Joy-Con" tokenizes to "joy con": treat the pair as one marker.
    if "joy" in offer_toks and "con" in offer_toks and "joy" not in query_toks:
        hits.append("joy-con")
    return hits


def _is_sequel_numeral(tok: str) -> bool:
    return (tok in ROMAN and tok not in ("v", "x")) or (tok.isdigit() and len(tok) == 1
                                                         and tok != "0" and tok != "1")


def sequel_of(offer_title: str, a: Anchors) -> bool:
    """True when the offer is a numbered sequel of what was asked.

    Asking for "Hades" must not accept "Hades II": the sequel contains every
    token of the original. When the requested title carries no numeral of its
    own, a numeral right after the name token marks a different game. "v" and
    "x" are skipped because they are common single letters ("Yakuza X",
    "Elden Ring V").
    """
    if any(m.startswith("#") for m in a.must):
        return False
    name = next((m for m in a.must if not m.startswith("#")), "")
    toks = tokens(offer_title)
    for i, t in enumerate(toks[:-1]):
        if t == name and _is_sequel_numeral(toks[i + 1]):
            return True
    return False


@dataclass(frozen=True)
class MatchResult:
    ok: bool
    reason: str            # human readable, stored for auditing
    score: int = 0         # higher = more confident it is the game itself


def match_offer(offer_title: str, query_title: str) -> MatchResult:
    a = anchors(query_title)
    if not a:
        return MatchResult(False, "query has no distinctive tokens")
    if not has_anchors(offer_title, a):
        return MatchResult(False, f"missing anchors {list(a.must)}")
    if sequel_of(offer_title, a):
        return MatchResult(False, "numbered sequel of the requested title")
    bad = negative_hits(offer_title, query_title)
    if bad:
        return MatchResult(False, f"negative markers {bad}")
    norm = normalize(offer_title)
    score = 1 + sum(1 for p in POSITIVE_PHRASES if p in norm)
    if norm.startswith("jogo "):
        score += 1
    return MatchResult(True, "anchors present, no negative markers", score)


# ----------------------------------------------------------------- price band

def price_band(prices: list[int], reference_cents: int | None = None,
               low: float = 0.35, high: float = 1.25) -> tuple[int, int] | None:
    """Plausible price range for a product, or None when nothing can be said.

    With a reference (a real store price) the band is a fixed fraction of it.
    Without one, the median of the matched listings estimates the product and
    the cut is symmetric: noise shows up on both sides, a R$ 16 faceplate and a
    R$ 4.700 console bundle, and a one-sided cut only catches one of them.
    Fewer than three listings cannot support a median, so no band is returned.
    """
    if reference_cents:
        return int(reference_cents * low), int(reference_cents * high)
    if len(prices) < 3:
        return None
    m = statistics.median(prices)
    return int(m / 3), int(m * 3)


def within_band(price: int, band: tuple[int, int] | None) -> bool:
    return band is None or band[0] <= price <= band[1]
