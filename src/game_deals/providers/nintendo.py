"""Nintendo — Switch e Switch 2, com preço em BRL.

Três fontes, cada uma pelo que faz melhor (todas verificadas, nenhuma exige chave):

1. **Catálogo europeu (Solr)** — `search.nintendo-europe.com`. É o único que conhece
   o Switch 2: `system_type:nintendoswitch2` traz 552 títulos, com capa, data e
   classificação. Usado só para DESCOBRIR e CLASSIFICAR.
2. **Catálogo pt-br (Algolia)** — tem NSUID das Américas, que é o que a eShop
   brasileira precifica. Mas é uma geração antiga do índice: **não tem Switch 2**.
3. **Página pt-br do produto** — para Switch 2, o NSUID brasileiro só aparece aqui.

O detalhe que custa caro se ignorado: **NSUID europeu não precifica no Brasil.**
O NSUID 70010000096802 (Mario Kart World, catálogo EU) responde `not_found` em
country=BR, mas cotou £66,99 em GB. O NSUID das Américas do mesmo jogo
(70010000095431) cotou R$ 439,90. São espaços de id por região.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata

from ..models import Listing, Offer
from .. import config
from .base import cents, client

SOLR = "https://search.nintendo-europe.com/en/select"
PRICE_URL = "https://api.ec.nintendo.com/v1/price"
LOJA_BR = "https://www.nintendo.com/pt-br/store/products"

ALGOLIA_APP = os.environ.get("NINTENDO_ALGOLIA_APP_ID", "U3B6GR4UA3")
ALGOLIA_KEY = os.environ.get("NINTENDO_ALGOLIA_KEY",
                             "c4da8be7fd29f0f5bfa42920b0a99dc7")
ALGOLIA_INDEX = os.environ.get("NINTENDO_ALGOLIA_INDEX", "ncom_game_pt_br")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

_NSUID = re.compile(r"^\d{14}$")
_NSUID_NA_PAGINA = re.compile(r'"nsuid"\s*:\s*"?(\d{14})')

# --------------------------------------------------------------- compatibilidade

NATIVO = "switch2_nativo"
EDICAO = "switch2_edition"
RETRO = "switch1_retrocompativel"

ROTULO = {
    NATIVO: "Switch 2 (nativo)",
    EDICAO: "Switch 2 Edition (upgrade)",
    RETRO: "Switch 1 — roda no Switch 2",
}


def compatibilidade(titulo: str, system_type: str) -> str:
    """Como o jogo roda num Switch 2.

    O Switch 2 é retrocompatível, então um jogo de Switch 1 *roda* — só não é
    nativo. "Nintendo Switch 2 Edition" é o meio-termo: jogo de Switch 1 com
    pacote de melhoria pago para o console novo.
    """
    if "switch 2 edition" in titulo.lower().replace("™", ""):
        return EDICAO
    if "nintendoswitch2" in (system_type or "").lower():
        return NATIVO
    return RETRO


# Os titulos de Switch 2 nao preenchem `image_url` (o campo classico) — a arte
# deles vive nos campos por proporcao. 16x9 primeiro: e o formato do card.
_CAMPOS_IMAGEM = ("image_url_h16x9_s", "image_url_h2x1_s", "image_url",
                  "image_url_sq_s")


def _imagem(doc: dict) -> str:
    for campo in _CAMPOS_IMAGEM:
        v = doc.get(campo)
        if isinstance(v, list):
            v = v[0] if v else ""
        if v:
            return f"https:{v}" if str(v).startswith("//") else str(v)
    return ""


def _slug_br(titulo: str, compat: str) -> str:
    """Slug da loja pt-br, derivado do título. É *derivado*, não adivinhado às
    cegas: o fetch valida contra a loja e devolve vazio se não existir."""
    t = titulo.replace("™", "").replace("®", "").replace("©", "")
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    # Apostrofo SOME (a Nintendo usa "no-mans-sky", nao "no-man-s-sky")
    t = t.replace("'", "").replace("\u2019", "")
    t = re.sub(r"[^a-zA-Z0-9]+", "-", t).strip("-").lower()
    sufixo = "switch-2" if compat in (NATIVO, EDICAO) else "switch"
    if sufixo in t:
        return t
    return f"{t}-{sufixo}"


class Nintendo:
    name = "nintendo"
    label = "Nintendo eShop (Switch / Switch 2)"

    def configured(self) -> bool:
        return True

    def why_unconfigured(self) -> str:
        return ""

    # ------------------------------------------------------------ descoberta

    def search(self, query: str, limit: int = 10,
               apenas_switch2: bool = False) -> list[Listing]:
        """Descobre e classifica. **Não ordena por relevância** — este catálogo
        não tem sinal de popularidade: `hits_i` vale 300 para todos os títulos,
        é constante. Ordenar por ele dá uma lista arbitrária com cara de ranking,
        que é pior do que nenhuma ordem. Para ranquear por nota × popularidade,
        use `ratings.top()` (precisa da RAWG_API_KEY).
        """
        fq = "type:GAME"
        if apenas_switch2:
            fq += " AND system_type:*switch2*"
        params = {"q": query or "*", "fq": fq, "wt": "json",
                  "rows": min(limit, 40)}

        try:
            with client() as c:
                r = c.get(SOLR, params=params)
                if r.status_code >= 400:
                    return []
                docs = (r.json().get("response") or {}).get("docs", [])
        except Exception:
            return []

        out: list[Listing] = []
        for d in docs:
            titulo = d.get("title", "")
            st = (d.get("system_type") or [""])[0]
            compat = compatibilidade(titulo, st)
            # O source_id é o slug pt-br: o NSUID europeu do Solr NÃO precifica
            # no Brasil, então guardá-lo aqui seria uma armadilha.
            out.append(Listing(
                source=self.name, source_id=_slug_br(titulo, compat),
                title=titulo,
                url=f"{LOJA_BR}/{_slug_br(titulo, compat)}/",
                image=_imagem(d),
                extra={
                    "compat": compat, "compat_rotulo": ROTULO[compat],
                    "publisher": d.get("publisher"),
                    "lancamento": d.get("pretty_date_s"),
                    "populares_hits": d.get("hits_i"),
                    "nsuid_eu": (d.get("nsuid_txt") or [None])[0],
                },
            ))
        return out

    def search_br(self, query: str, limit: int = 10) -> list[Listing]:
        """Catálogo pt-br: devolve NSUID das Américas, que precifica em BRL.
        Não conhece Switch 2 — use `search()` para esses."""
        url = f"https://{ALGOLIA_APP}-dsn.algolia.net/1/indexes/{ALGOLIA_INDEX}/query"
        headers = {"X-Algolia-API-Key": ALGOLIA_KEY,
                   "X-Algolia-Application-Id": ALGOLIA_APP,
                   "Content-Type": "application/json"}
        try:
            with client(headers=headers) as c:
                r = c.post(url, json={"query": query, "hitsPerPage": limit})
                if r.status_code >= 400:
                    return []
                hits = (r.json() or {}).get("hits", [])
        except Exception:
            return []
        out = []
        for h in hits:
            if not h.get("nsuid"):
                continue
            u = h.get("url", "")
            out.append(Listing(
                source=self.name, source_id=str(h["nsuid"]), title=h.get("title", ""),
                url=f"https://www.nintendo.com{u}" if u.startswith("/") else u,
                image=h.get("horizontalHeaderImage", ""),
                extra={"compat": RETRO, "compat_rotulo": ROTULO[RETRO],
                       "plataforma": h.get("platform")}))
        return out

    # ------------------------------------------------------------ preço

    def nsuid_br(self, slug: str) -> str:
        """NSUID das Américas a partir do slug da loja pt-br. Para Switch 2 este
        é o único caminho — o índice pt-br ainda não tem esses títulos."""
        try:
            with client(headers={"User-Agent": UA,
                                 "Accept": "text/html"}) as c:
                r = c.get(f"{LOJA_BR}/{slug.strip('/')}/")
                if r.status_code >= 400:
                    return ""
                m = _NSUID_NA_PAGINA.search(r.text)
                return m.group(1) if m else ""
        except Exception:
            return ""

    def fetch(self, source_id: str) -> list[Offer]:
        """Aceita NSUID das Américas (14 dígitos) ou o slug da loja pt-br."""
        sid = source_id.strip()
        slug = ""
        if not _NSUID.match(sid):
            slug = sid
            sid = self.nsuid_br(slug)
            if not sid:
                return []

        with client() as c:
            r = c.get(PRICE_URL, params={"country": config.COUNTRY, "lang": "pt",
                                         "ids": sid})
            if r.status_code >= 400:
                return []
            data = r.json() or {}

        offers: list[Offer] = []
        for p in data.get("prices", []) or []:
            status = p.get("sales_status")
            if status in ("not_found", "unreleased"):
                continue
            regular = (p.get("regular_price") or {}).get("raw_value")
            disc = p.get("discount_price") or {}
            price_c = cents(disc.get("raw_value") or regular)
            if price_c is None:
                continue
            offers.append(Offer(
                source=self.name, source_id=source_id,
                title="Nintendo eShop", store="Nintendo eShop",
                price_cents=price_c, regular_cents=cents(regular),
                currency=(p.get("regular_price") or {}).get("currency", config.CURRENCY),
                url=(f"{LOJA_BR}/{slug}/" if slug else
                     f"https://www.nintendo.com/pt-br/store/products/?nsuid={sid}"),
                extra={"sales_status": status, "nsuid_br": sid,
                       "promo_ate": disc.get("end_datetime")},
            ))
        return offers


provider = Nintendo()
