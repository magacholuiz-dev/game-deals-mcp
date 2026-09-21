"""O veredito e a unica logica com risco real de estar sutilmente errada,
entao ela e a que tem teste."""
import os
import tempfile

import pytest

os.environ["GAMEDEALS_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")

from game_deals import db          # noqa: E402
from game_deals.verdict import evaluate, _humanize   # noqa: E402

DAY = 86400


@pytest.fixture(autouse=True)
def limpa():
    db.conn().executescript(
        "DELETE FROM price_points; DELETE FROM aliases; "
        "DELETE FROM watches; DELETE FROM alerts; DELETE FROM products;")
    db.upsert_product("p", "Jogo Teste")
    yield


def ponto(price_cents, dias_atras, store="Loja"):
    db.record("p", "fonte", store, price_cents, None, "BRL", True, "",
              ts=db.now() - dias_atras * DAY)


def test_primeira_leitura_nao_finge_ter_historico():
    ponto(30000, 0)
    v = evaluate("p", "Loja", 30000)
    assert v.samples == 1
    assert v.confidence == "baixa"
    assert "sem histórico" in v.label


def test_minimo_historico():
    ponto(30000, 400)
    ponto(25000, 100)
    ponto(20000, 0)
    v = evaluate("p", "Loja", 20000)
    assert v.is_all_time_low
    assert v.days_since_lower is None
    assert v.min_all_cents == 20000


def test_dias_desde_preco_menor():
    ponto(15000, 200)   # ja esteve a 150 ha 200 dias
    ponto(30000, 100)
    ponto(20000, 0)
    v = evaluate("p", "Loja", 20000)
    assert not v.is_all_time_low
    assert v.days_since_lower == 200
    assert "7 meses" in v.label


def test_janelas_90_e_365_dias():
    ponto(10000, 500)   # fora das duas janelas
    ponto(18000, 200)   # so dentro de 365d
    ponto(22000, 30)    # dentro de 90d
    v = evaluate("p", "Loja", 22000)
    assert v.min_90d_cents == 22000
    assert v.min_365d_cents == 18000
    assert v.min_all_cents == 10000


def test_escopo_por_loja_nao_mistura_lojas():
    ponto(10000, 60, store="Amazon")         # Amazon ja esteve a R$ 100
    ponto(30000, 60, store="Shopee")
    ponto(28000, 30, store="Shopee")
    v_shopee = evaluate("p", "Shopee", 25000, scope_store=True)
    assert v_shopee.is_all_time_low          # menor preco DA SHOPEE
    v_geral = evaluate("p", "Shopee", 25000, scope_store=False)
    assert not v_geral.is_all_time_low       # mas a Amazon ja esteve mais barata
    assert v_geral.days_since_lower == 60


def test_cold_start_nunca_declara_minimo_historico():
    """Um unico ponto nao autoriza dizer 'menor preco ja visto'."""
    ponto(30000, 10, store="Shopee")
    v = evaluate("p", "Shopee", 25000)
    assert not v.is_all_time_low
    assert v.confidence == "baixa"


def test_confianca_sobe_com_historico():
    for d in range(0, 120, 3):
        ponto(30000 + d, d)
    v = evaluate("p", "Loja", 40000)
    assert v.confidence == "alta"
    assert v.caveat == ""


@pytest.mark.parametrize("dias,esperado", [
    (10, "10 dias"), (44, "44 dias"), (90, "3 meses"),
    (365, "1 ano"), (418, "1 ano e 2 meses"), (760, "2 anos e 1 mês"),
])
def test_humanize(dias, esperado):
    assert _humanize(dias) == esperado


def test_preco_que_subiu_nao_e_vendido_como_promocao():
    """Regressao: preco cheio hoje, mais barato ha 40 dias. A janela de 40 dias
    so contem a leitura de hoje, entao 'menor preco dos ultimos 40 dias' seria
    tecnicamente verdade e completamente enganoso."""
    ponto(19900, 200)
    ponto(26400, 40)
    ponto(32990, 0)
    v = evaluate("p", "Loja", 32990)
    assert v.above_recent_min
    assert "não é promoção" in v.label
    assert "264,00" in v.label
    assert "menor preço dos últimos" not in v.label


def test_promocao_de_verdade_mantem_a_frase_boa():
    ponto(19900, 400)
    ponto(32990, 40)
    ponto(24000, 0)      # abaixo do minimo de 90d (32990), acima do ATL
    v = evaluate("p", "Loja", 24000)
    assert not v.above_recent_min
    assert v.label.startswith("menor preço dos últimos")


# ---------------------------------------------------------------- selo

from game_deals.verdict import highlight   # noqa: E402


def test_selo_nao_aparece_sem_historico():
    ponto(30000, 0)
    assert highlight(evaluate("p", "Loja", 30000)) is None


def test_selo_nao_aparece_com_preco_acima_do_minimo_recente():
    ponto(20000, 60)
    ponto(30000, 30)
    ponto(35000, 0)
    assert highlight(evaluate("p", "Loja", 35000)) is None


def test_selo_historico():
    for d in range(0, 200, 5):
        ponto(30000, d)
    h = highlight(evaluate("p", "Loja", 19900))
    assert h["tier"] == "historico"


def test_selo_destaque_por_janela_longa():
    ponto(19900, 400)                      # ja foi mais barato ha muito tempo
    for d in range(0, 200, 5):
        ponto(30000, d)
    h = highlight(evaluate("p", "Loja", 24000))
    assert h["tier"] == "destaque"
    assert "1 ano" in h["detalhe"]


# ---------------------------------------------------------------- relevância

from game_deals.ratings import relevancia   # noqa: E402


def test_relevancia_equilibra_nota_e_popularidade():
    """O ponto da fórmula: um jogo excelente e obscuro não pode ser esmagado por
    um mediano e popularíssimo — nem o contrário."""
    obscuro_otimo = relevancia(97, None, 300)
    popular_mediano = relevancia(72, None, 19000)
    assert abs(obscuro_otimo - popular_mediano) < 5      # ficam no mesmo patamar

    blockbuster = relevancia(97, None, 19000)
    assert blockbuster > obscuro_otimo
    assert blockbuster > popular_mediano


def test_escala_log_impede_popularidade_de_dominar():
    """Sem log, 19000 adds contra 1000 achataria a nota para irrelevante."""
    a = relevancia(60, None, 19000)      # nota fraca, popularidade máxima
    b = relevancia(95, None, 1000)       # nota alta, popularidade modesta
    assert b > a


def test_relevancia_cai_para_nota_de_usuarios_sem_metacritic():
    assert relevancia(None, 4.5, 5000) > relevancia(None, 2.0, 5000)


def test_relevancia_sem_nota_nenhuma_nao_quebra():
    assert 0 <= relevancia(None, None, 0) <= 100


# ---------------------------------------------------------------- alertas

from game_deals.alerts import evaluate as regra   # noqa: E402
from game_deals.models import Offer               # noqa: E402


def _oferta(preco_cents, regular=None):
    return Offer(source="f", source_id="x", title="t", store="Loja",
                 price_cents=preco_cents, regular_cents=regular)


def test_new_low_nao_dispara_no_primeiro_dia():
    """Regressão: produto recém-cadastrado tem 2 leituras do mesmo dia. Chamar
    isso de 'menor preço já registrado' dispararia alerta em todo item novo."""
    ponto(30000, 1)
    ponto(29900, 0)
    v = evaluate("p", "Loja", 29900)
    assert v.is_all_time_low            # tecnicamente verdade...
    assert not v.enough_history
    assert regra("new_low", None, _oferta(29900), v, None) is None   # ...mas não avisa
    assert highlight(v) is None                                      # nem sela


def test_new_low_dispara_com_historico_real():
    for d in range(30, 200, 4):
        ponto(30000, d)
    v = evaluate("p", "Loja", 19900)
    assert v.confidence == "alta"
    msg = regra("new_low", None, _oferta(19900), v, None)
    assert msg and "199,00" in msg


def test_price_below_dispara_mesmo_sem_historico():
    """Alvo explícito é decisão sua — não depende de histórico para valer."""
    ponto(30000, 0)
    v = evaluate("p", "Loja", 19900)
    assert regra("price_below", 250.0, _oferta(19900), v, None) is not None


def test_frase_de_minimo_historico_exige_base():
    """Selo, alerta e TEXTO usam a mesma guarda — não podem discordar entre si."""
    ponto(30000, 1)
    ponto(29900, 0)
    v = evaluate("p", "Loja", 29900)
    assert "menor preço já registrado" not in v.label
    assert "sem base de comparação" in v.label
    assert highlight(v) is None
    assert regra("new_low", None, _oferta(29900), v, None) is None


# ---------------------------------------------------------------- identidade

import sys                                                    # noqa: E402
from pathlib import Path as _P                                # noqa: E402
sys.path.insert(0, str(_P(__file__).resolve().parents[1] / "scripts"))
from setup_watchlist import mesmo_jogo, melhor_candidato      # noqa: E402


class _Ach:
    def __init__(self, title):
        self.title, self.source_id = title, title.lower()


@pytest.mark.parametrize("rawg,loja,esperado", [
    ("Rocket League", "Rugby League 26", False),          # o bug original
    ("Mario Kart World", "Mario Kart 8 Deluxe", False),
    ("Hades", "Hades II", False),                          # sequência
    ("Super Meat Boy", "Super Meat Boy 3D", False),
    ("Diablo IV", "Diablo III Eternal Collection", False),
    ("The Witcher 3: Wild Hunt", "The Witcher 3: Wild Hunt — Remastered", True),
    ("Hades", "Hades - Nintendo Switch 2 Edition", True),
])
def test_identidade_de_jogo(rawg, loja, esperado):
    assert mesmo_jogo(rawg, loja) is esperado


def test_escolhe_o_candidato_mais_proximo():
    achados = [_Ach("Hades - Nintendo Switch 2 Edition"), _Ach("Hades")]
    assert melhor_candidato("Hades", achados).title == "Hades"


def test_sem_candidato_valido_devolve_none():
    assert melhor_candidato("Rocket League", [_Ach("Rugby League 26")]) is None


def test_corte_por_mediana_e_simetrico():
    """Regressão: o corte só pegava caro demais. O GTA VI aparecia a R$ 15,83
    porque um faceplate de PS5 casava com a busca e passava por baixo."""
    from game_deals.providers.promobit import Sinal

    def s(cents, titulo="x"):
        return Sinal(source="promobit", source_id=str(cents), titulo=titulo,
                     loja="L", price_cents=cents, old_price_cents=None,
                     desconto_pct=0, url="", imagem="", publicado_ts=0,
                     curtidas=0, comentarios=0)

    sinais = [s(1583, "faceplate"), s(34417), s(35565), s(37640), s(699900, "console")]
    precos = sorted(x.price_cents for x in sinais)
    mediana = precos[len(precos) // 2]
    sobrou = [x for x in sinais if mediana / 3 <= x.price_cents <= mediana * 3]
    titulos = {x.titulo for x in sobrou}
    assert "faceplate" not in titulos      # cortado por baixo
    assert "console" not in titulos        # cortado por cima
    assert len(sobrou) == 3


def test_casamento_de_titulo_rejeita_jogo_diferente():
    """Regressão: o Solr da Nintendo devolve 'Mario Kart World' ao buscar
    'Mario Kart 8 Deluxe'. Aceitar o 1º resultado colava R$ 439,90 do World no
    card do 8 Deluxe."""
    from game_deals.wishlist import _bate
    assert _bate("Mario Kart 8 Deluxe", "Mario Kart 8 Deluxe")
    assert not _bate("Mario Kart 8 Deluxe", "Mario Kart World")
    assert not _bate("Grand Theft Auto VI", "Grand Theft Auto V")
    assert _bate("Grand Theft Auto VI", "Jogo Grand Theft Auto VI Standard")
    assert not _bate("The Legend of Zelda: Ocarina of Time",
                     "The Legend of Zelda: Breath of the Wild")



def test_preco_que_nunca_mudou_nao_e_minimo_historico():
    """Regressão: um jogo sempre a R$ 250 recebia o selo dourado e o alerta de
    'menor preço', porque ninguém jamais foi mais barato. Preço estável não é
    oferta."""
    for d in range(0, 120, 4):
        ponto(25000, d)
    v = evaluate("p", "Loja", 25000)
    assert not v.is_all_time_low
    assert "sem variação" in v.label
    assert highlight(v) is None
    assert regra("new_low", None, _oferta(25000), v, None) is None


def test_voltar_ao_minimo_depois_de_ter_estado_mais_caro_continua_sendo_minimo():
    for d in range(0, 120, 4):
        ponto(30000, d)
    v = evaluate("p", "Loja", 25000)
    assert v.is_all_time_low
