"""Monta a watchlist no banco REAL (deals.db). Idempotente.

**O ranking depende da RAWG_API_KEY.** Sem ela não existe, em nenhuma fonte
gratuita, sinal de nota ou de vendas: o catálogo da Nintendo tem `hits_i`
constante em 300 para todo título, e a PS Store não expõe busca. Sem a chave
este script registra apenas uma lista curada à mão e **avisa** que ela não é
ranqueada — em vez de entregar uma ordem arbitrária com cara de ranking.

    uv run python scripts/setup_watchlist.py
"""
from __future__ import annotations

import os
import re
import unicodedata

os.environ.setdefault("GAMEDEALS_DB", "./deals.db")

from game_deals import db, providers, ratings                    # noqa: E402
from game_deals.collector import coletar_sinais, refresh_product  # noqa: E402
from game_deals.models import brl                                # noqa: E402
from game_deals.providers.nintendo import NATIVO                 # noqa: E402

# GTA VI lança 19/11/2026: pré-venda NÃO entra em desconto — regra é `any_drop`.
FIXOS = [
    ("playstation", "product/EP1004-PPSA01547_00-GTAVISTANDARD001",
     "Grand Theft Auto VI", "ps5", "any_drop"),
    ("playstation", "10000730",
     "Grand Theft Auto VI: Ultimate Edition", "ps5", "any_drop"),
]

# (título, categoria, plataforma, teto R$) — sem loja com API não há preço de
# referência, e sem referência o feed traz console junto com controle.
SO_COMUNIDADE = [("Controle DualSense GTA VI", "hardware", "ps5", 700.0)]

POR_PLATAFORMA = 6
MIN_METACRITIC = 78

# O RAWG ordena por "adicionado à biblioteca", que é acumulado de todos os
# tempos: sem recorte, o top de PS5 vira Skyrim (2011) e GTA V (2013) — ótimos,
# mas não é o que "os maiores do PS5" quer dizer. Recortamos pela geração.
#
# No Switch 2 NÃO recortamos: o RAWG guarda a data do lançamento original, então
# filtrar por data cortaria justamente as "Switch 2 Edition" (Witcher 3, Hades),
# que são o grosso do catálogo bom do console. Ali quem garante a plataforma é o
# cruzamento com o catálogo da Nintendo.
DESDE = {"ps5": "2020-11-12"}


_PALAVRA_FRACA = {"the", "a", "o", "of", "de", "da", "do", "and", "e",
                  "edition", "remastered", "complete", "definitive", "hd"}
_NUMERAIS = {"2", "3", "4", "ii", "iii", "iv", "v", "vi", "3d", "2d"}
_ROMANO = re.compile(r"(?:i{2,3}|iv|vi{0,3}|ix|xi{0,2})")


# "Nintendo Switch 2 Edition" é nome de plataforma, não parte do título — e o
# "2" dele seria lido como marcador de sequência, rejeitando o jogo certo.
_PLATAFORMA_NO_TITULO = re.compile(
    r"\s*[-–—]?\s*(?:for\s+)?nintendo\s+switch\s*2?\s*(?:edition)?"
    r"|\s*[-–—]\s*switch\s*2?\s*(?:edition)?", re.I)


def _tokens(titulo: str) -> set[str]:
    t = titulo.replace("™", "").replace("®", "")
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    t = _PLATAFORMA_NO_TITULO.sub(" ", t)
    t = t.replace("'", "").replace("\u2019", "").lower()
    return {w for w in re.split(r"[^a-z0-9]+", t)
            if w and w not in _PALAVRA_FRACA}


def mesmo_jogo(titulo_rawg: str, titulo_loja: str) -> bool:
    """Todo token significativo do título do RAWG tem de aparecer no da loja.

    Pegar o primeiro resultado da busca é o erro clássico aqui: "Rocket League"
    casava com "Rugby League 26" — mesma segunda palavra, jogo completamente
    diferente, e o card mostraria R$ 439,90 de um jogo que é gratuito. Um alerta
    no produto errado destrói a confiança na lista inteira.
    """
    a, b = _tokens(titulo_rawg), _tokens(titulo_loja)
    if not a or not a <= b:
        return False
    # Subconjunto sozinho aceita sequência: "Hades" ⊆ "Hades II", "Super Meat
    # Boy" ⊆ "Super Meat Boy 3D", "Diablo" ⊆ "Diablo III". Se o que sobra traz
    # numeral que o original não tem, é outro jogo.
    extra = b - a
    return not (extra & _NUMERAIS) and not any(
        w.isdigit() or _ROMANO.fullmatch(w) for w in extra)


def melhor_candidato(titulo_rawg: str, achados: list) -> object | None:
    """Entre os que passam, o mais próximo — menos palavras sobrando."""
    validos = [x for x in achados if mesmo_jogo(titulo_rawg, x.title)]
    if not validos:
        return None
    alvo = _tokens(titulo_rawg)
    # Desempate por título mais curto: com a plataforma removida, "Hades" e
    # "Hades - Nintendo Switch 2 Edition" empatam em tokens extras. Ficar com o
    # mais curto torna a escolha determinística em vez de depender da ordem em
    # que a busca devolveu.
    return min(validos,
               key=lambda x: (len(_tokens(x.title) - alvo), len(x.title)))


def slug(titulo: str) -> str:
    t = unicodedata.normalize("NFKD", titulo).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")[:44]


def _finaliza(pid: str, titulo: str, regra: str, ofertas, sinais,
              silencioso: bool = False) -> bool:
    if not ofertas and not sinais:
        if not silencioso:
            print(f"  ! {titulo[:42]:44} sem preço na eShop nem no varejo — pulado")
        for t in ("aliases", "price_points"):
            db.conn().execute(f"DELETE FROM {t} WHERE product_id=?", (pid,))
        db.conn().execute("DELETE FROM products WHERE id=?", (pid,))
        db.conn().commit()
        return False
    if not any(w["product_id"] == pid for w in db.active_watches()):
        db.add_watch(pid, regra, None, "setup_watchlist")
    if ofertas:
        print(f"  ✓ {titulo[:42]:44} {brl(min(o.price_cents for o in ofertas)):>12}  eShop")
    else:
        print(f"  ✓ {titulo[:42]:44} {'—':>12}  varejo ({len(sinais)} ofertas)")
    return True


def entra_loja(titulo: str, fonte: str, sid: str, plataforma: str,
               compat: str, regra: str, imagem: str = "",
               silencioso: bool = False) -> bool:
    pid = slug(titulo)
    db.upsert_product(pid, titulo, "game", plataforma, imagem, compat)
    db.add_alias(pid, fonte, sid)
    ofertas = refresh_product(pid, quiet=True)
    sinais = coletar_sinais(pid, titulo) if not ofertas else []
    return _finaliza(pid, titulo, regra, ofertas, sinais, silencioso)


def entra_varejo(titulo: str, plataforma: str, regra: str,
                 imagem: str = "") -> bool:
    """Sem loja com API (todo PS5 que não seja concept id conhecido): o preço vem
    do varejo brasileiro via Promobit."""
    pid = slug(titulo)
    db.upsert_product(pid, titulo, "game", plataforma, imagem)
    sinais = coletar_sinais(pid, titulo)
    return _finaliza(pid, titulo, regra, [], sinais)


def ranqueados() -> None:
    for plataforma in ("ps5", "switch2"):
        fichas = ratings.top(plataforma, MIN_METACRITIC, POR_PLATAFORMA * 4,
                             DESDE.get(plataforma, ""))
        print(f"\n{plataforma.upper()} — top por nota × popularidade "
              f"(min. Metacritic {MIN_METACRITIC})")
        entraram = 0
        for f in fichas:
            if entraram >= POR_PLATAFORMA:
                break
            print(f"    [{f.metacritic or '--'}] relev {f.relevancia:>5}  "
                  f"{f.popularidade:>6} jogadores  {f.titulo[:40]}")
            ok = False
            if plataforma == "switch2":
                # O RAWG não conhece Switch 2: quem confirma a plataforma é o
                # catálogo da Nintendo. Sem esse cruzamento, entraria jogo que
                # nunca saiu no console.
                escolhido = melhor_candidato(
                    f.titulo,
                    providers.get("nintendo").search(f.titulo, 8,
                                                     apenas_switch2=True))
                if escolhido is None:
                    print("       (não existe no Switch 2 — pulado)")
                    continue
                ok = entra_loja(f.titulo, "nintendo", escolhido.source_id,
                                plataforma, NATIVO, "new_low", f.imagem,
                                silencioso=True)
            if not ok:
                ok = entra_varejo(f.titulo, plataforma, "new_low", f.imagem)
            if ok:
                pid = slug(f.titulo)
                db.set_ratings(pid, f.rawg_id, f.metacritic, f.nota_usuarios,
                               f.avaliacoes, f.popularidade, f.relevancia,
                               f.lancamento)
                entraram += 1


def sem_ranking() -> None:
    print("\n" + "!" * 72)
    print("! RAWG_API_KEY ausente — NÃO há como ranquear por nota ou vendas.")
    print("! O catálogo da Nintendo tem hits_i=300 em todo título (constante) e")
    print("! a PS Store não expõe busca. Qualquer 'top' aqui seria arbitrário.")
    print("! Pegue a chave grátis em https://rawg.io/apidocs e rode de novo:")
    print("!   echo 'RAWG_API_KEY=sua_chave' >> .env")
    print("!" * 72)
    print("\nRegistrando só os fixos (GTA VI) — sem lista de 'maiores'.")


def main() -> None:
    print(f"banco: {os.environ['GAMEDEALS_DB']}\n")
    print("GTA VI (PS5) — pré-venda, lança 19/11/2026")
    for fonte, sid, titulo, plat, regra in FIXOS:
        entra_loja(titulo, fonte, sid, plat, "", regra)

    if ratings.configured():
        ranqueados()
    else:
        sem_ranking()

    print("\nAcessórios — só sinal da comunidade")
    for titulo, cat, plat, teto in SO_COMUNIDADE:
        pid = slug(titulo)
        db.upsert_product(pid, titulo, cat, plat)
        if not any(w["product_id"] == pid for w in db.active_watches()):
            db.add_watch(pid, "price_below", teto, "só comunidade")
        teto_c = int(teto * 100)
        n = coletar_sinais(pid, titulo, teto_c, int(teto_c * 0.15))
        print(f"  ✓ {titulo[:42]:44} {len(n)} ofertas (teto R$ {teto:.0f})")

    print(f"\n{len(db.list_products())} produtos, "
          f"{len(db.active_watches())} watches ativos.")
    if not ratings.configured():
        print("Adicione a RAWG_API_KEY e rode de novo para ter os maiores jogos.")


if __name__ == "__main__":
    main()
