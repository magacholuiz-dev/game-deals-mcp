"""Promobit — agregador brasileiro de ofertas.

Cobre de uma vez as lojas que não têm API pública: KaBuM!, Netshoes, Magazine
Luiza, Casas Bahia, Americanas, Fast Shop — além de Amazon e Shopee. Integrar
cada varejista separadamente daria muito mais trabalho e cobriria menos.

**Este provider é de outro tipo.** Os demais observam o preço de uma loja de
forma periódica; este lê ofertas *postadas por pessoas*. A diferença importa:

- um preço do Promobit aparece uma vez e some — não é uma série temporal;
- o preço postado pode estar errado, expirado ou ser de vendedor duvidoso;
- misturar isso no histórico envenenaria o veredito "menor preço em X tempo",
  que depende de leituras regulares da mesma loja.

Por isso `kind = "feed"`: o coletor grava estes registros em `deal_signals`,
nunca em `price_points`. Eles disparam alerta e aparecem no card como
"ofertas da comunidade", mas não entram no cálculo do histórico.
"""
from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass, asdict
from typing import Any

from .. import config
from ..models import Listing
from .base import cents, client

API = "https://api.promobit.com.br"
SITE = "https://www.promobit.com.br"
IMG = "https://i.promobit.com.br/400"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


@dataclass
class Sinal:
    """Uma oferta postada — sinal, não observação de preço."""
    source: str
    source_id: str
    titulo: str
    loja: str
    price_cents: int
    old_price_cents: int | None
    desconto_pct: float
    url: str
    imagem: str
    publicado_ts: int
    curtidas: int
    comentarios: int
    ativa: bool = True
    categoria: str = ""
    subcategoria: str = ""      # "Playstation 5", "Nintendo Switch", ...

    def dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["preco"] = self.price_cents / 100
        return d


# A busca do Promobit e literal: "Zelda: Breath of the Wild" devolve zero, mas
# "zelda" devolve dezenas. Consultamos do termo mais especifico ao mais largo e
# paramos no primeiro que responde — assim mantemos a precisao quando ela existe.
_RUIDO = {"de", "da", "do", "the", "of", "a", "o", "e", "-", "edition",
          "deluxe", "hd", "remaster", "definitive"}


def termos(titulo: str) -> list[str]:
    """Consultas da mais específica para a mais larga."""
    base = titulo.split(":")[0].split("–")[0].split(" - ")[0].strip()
    palavras = [w for w in re.split(r"[^\w]+", base.lower())
                if w and w not in _RUIDO]
    saida = []
    for n in (3, 2, 1):
        if len(palavras) >= n:
            t = " ".join(palavras[:n])
            if t not in saida:
                saida.append(t)
    return saida or [titulo.lower()]


# Acessorio da mesma marca casa com qualquer busca por titulo: "Marvel's
# Wolverine" trouxe um boneco Marvel Legends, "Ghost of Yotei" trouxe um teclado.
# Nenhum campo da API separa jogo de produto licenciado — a subcategoria de todos
# e "Playstation 5". Lista negativa e grosseira, mas e o que funciona aqui.
_ACESSORIO = (
    "controle", "control ", "capa ", "capinha", "case ", "kit ", "teclado",
    "mouse", "headset", "fone ", "suporte", "carregador", "cabo ", "dock",
    "pelicula", "película", "boneco", "figure", "funko", "camiseta", "caneca",
    "chaveiro", "mochila", "poster", "pôster", "adesivo", "skin ", "faceplate",
    "bateria", "memory card", "cartao de memoria", "hd externo", "ssd ",
    # Bundle de console com o jogo: casa com a ancora e custa 15x o jogo.
    # "ps5" sozinho NAO entra — "Jogo ... PS5" e exatamente o que queremos.
    "console", "playstation 5", "playstation5", "nintendo switch 2 +",
    # Bundle de Switch sempre cita o Joy-Con: "Nintendo Switch 32GB 1 Par
    # Joy-con + Mario Kart 8" custa 5x o jogo e casa com a busca do jogo.
    "joy-con", "joycon", "joy con",
)

# Numeral e o discriminante em seguencia: "Grand Theft Auto" casa com V e VI.
_ROMANO = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7",
           "viii": "8", "ix": "9", "x": "10"}


def _sem_acento(t: str) -> str:
    return unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()


def parece_acessorio(titulo_oferta: str) -> bool:
    t = _sem_acento(titulo_oferta)
    return any(p in t for p in _ACESSORIO)


def ancora_forte(titulo: str) -> list[str]:
    """Tokens que a oferta PRECISA conter. A ultima palavra significativa costuma
    ser o nome proprio do jogo ("yotei", "wolverine"); o numeral, quando existe,
    entra junto porque e ele que separa a sequencia."""
    palavras = [w for w in re.split(r"[^\w]+", _sem_acento(titulo))
                if w and w not in _RUIDO]
    if not palavras:
        return []
    exigidos = [palavras[-1]]
    for w in palavras:
        if w in _ROMANO or (w.isdigit() and len(w) <= 2):
            num = _ROMANO.get(w, w)
            exigidos = [p for p in exigidos if p != w]
            exigidos.append(f"#{num}")     # "#6" = aceita "VI" ou "6"
    return exigidos


def _tem_ancoras(titulo_oferta: str, exigidos: list[str]) -> bool:
    t = _sem_acento(titulo_oferta)
    tokens = set(re.split(r"[^\w]+", t))
    for e in exigidos:
        if e.startswith("#"):
            num = e[1:]
            romano = next((r for r, n in _ROMANO.items() if n == num), "")
            if num not in tokens and (not romano or romano not in tokens):
                return False
        elif e not in t:
            return False
    return True


def _combina(titulo_oferta: str, ancora: str) -> bool:
    """A consulta larga traz capinha, caneca e bundle. Exigir a âncora no título
    da oferta corta o pior do ruído — o teto de preço corta o resto."""
    a = unicodedata.normalize("NFKD", ancora).encode("ascii", "ignore").decode()
    t = unicodedata.normalize("NFKD", titulo_oferta).encode("ascii", "ignore").decode()
    return a.lower() in t.lower()


def _ts(s: str) -> int:
    if not s:
        return 0
    try:
        return int(dt.datetime.fromisoformat(s).timestamp())
    except ValueError:
        return 0


def _sinal(o: dict, ativa: bool = True) -> Sinal | None:
    preco = cents(o.get("offer_price"))
    if not preco:
        return None
    velho = cents(o.get("offer_old_price"))
    slug = o.get("offer_slug", "")
    foto = o.get("offer_photo") or ""
    return Sinal(
        source="promobit", source_id=str(o.get("offer_id", "")),
        titulo=o.get("offer_title", ""),
        loja=o.get("store_name", "") or "?",
        price_cents=preco,
        old_price_cents=velho if velho else None,
        desconto_pct=float(o.get("offer_discont_percentage") or 0),
        url=f"{SITE}/oferta/{slug}" if slug else SITE,
        imagem=f"{IMG}{foto}" if foto.startswith("/") else "",
        publicado_ts=_ts(o.get("offer_published", "")),
        curtidas=int(o.get("offer_likes") or 0),
        comentarios=int(o.get("offer_comments") or 0),
        ativa=ativa,
        categoria=o.get("category_name") or "",
        subcategoria=o.get("subcategory_name") or "",
    )


class Promobit:
    name = "promobit"
    label = "Promobit (ofertas da comunidade)"
    kind = "feed"          # <- não alimenta o histórico de preços

    def configured(self) -> bool:
        return True

    def why_unconfigured(self) -> str:
        return ""

    def sinais_do_titulo(self, titulo: str, limit: int = 12,
                         plataforma: str = "",
                         incluir_encerradas: bool = False,
                         ignorar_acessorios: bool = True) -> list[Sinal]:
        """Sinais para um produto, do termo mais específico ao mais largo.

        A âncora é o termo que efetivamente casou — não o mais largo. Usar o mais
        largo deixava passar acessório: buscando "grand theft auto", uma capinha
        de PS5 entrava porque continha "GTA" no título. Exigindo o próprio termo
        que trouxe o resultado, ela cai fora.
        """
        exigidos = ancora_forte(titulo)
        for t in termos(titulo):
            achados = [s for s in self.sinais(t, limit * 3, incluir_encerradas)
                       if _combina(s.titulo, t)
                       and _tem_ancoras(s.titulo, exigidos)
                       and not (ignorar_acessorios and parece_acessorio(s.titulo))]
            if plataforma:
                achados = [s for s in achados
                           if not s.subcategoria
                           or _combina(s.subcategoria, plataforma)]
            if achados:
                return achados[:limit]
        return []

    def sinais(self, query: str, limit: int = 20,
               incluir_encerradas: bool = False) -> list[Sinal]:
        try:
            with client(headers={"User-Agent": UA, "Referer": SITE + "/"}) as c:
                r = c.get(f"{API}/search", params={"q": query})
                if r.status_code >= 400:
                    return []
                d = r.json() or {}
        except Exception:
            return []

        pares = [(o, True) for o in (d.get("active_offers") or [])]
        if incluir_encerradas:
            # Ofertas encerradas nao servem para comprar hoje, mas sao precos
            # REAIS observados com data — o unico historico gratuito que existe
            # para varejo brasileiro. Entram marcadas, e o card as separa.
            pares += [(o, False) for o in (d.get("finished_offers") or [])]

        out = [s for s in (_sinal(o, ativa) for o, ativa in pares) if s]
        out.sort(key=lambda s: s.price_cents)
        return out[:limit]

    def search(self, query: str, limit: int = 10) -> list[Listing]:
        return [
            Listing(source=self.name, source_id=s.source_id, title=s.titulo,
                    url=s.url, image=s.imagem, price_cents=s.price_cents,
                    extra={"loja": s.loja, "desconto": s.desconto_pct})
            for s in self.sinais(query, limit)
        ]

    def fetch(self, source_id: str) -> list:
        """Feed não tem preço-por-id observável no tempo: um post é um evento,
        não uma série. O coletor usa `sinais()`."""
        return []


provider = Promobit()
