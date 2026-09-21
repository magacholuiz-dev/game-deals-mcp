"""Opportunity score, budget planner, edition / upgrade / media comparison."""
import datetime as dt
import itertools
import random

import pytest

from game_deals import db, intel
from tests.test_intel import NOW, QUIET, comparables, history, product, seed, ts, D

# A date with nothing on the sale calendar, so plans are not held back by an event.
TODAY = dt.date(2026, 8, 3)


@pytest.fixture(autouse=True)
def _db(scratch_db):
    pass


def rate(pid, metacritic=None, popularity=0, playtime=None, priority=0, user=None):
    db.set_ratings(pid, None, metacritic, user, 0, popularity, None, "")
    db.set_playtime(pid, playtime)
    db.set_priority(pid, priority)


# ================================================================== score

def test_score_is_none_without_a_current_price():
    product("p")
    op = intel.pontuar_oportunidade("p", NOW)
    assert op.score is None and op.cobertura == 0.0


def test_discount_is_measured_against_the_typical_price_not_the_printed_list_price():
    """The store prints R$ 600 as the regular price. The buyer has been seeing
    R$ 300. A 10% cut from 300 is a small deal, not 55%."""
    product("p")
    history("p", "L", [(d, 30000) for d in range(120, 1, -6)], regular=60000)
    seed("p", "L", 27000, 0.1, regular=60000)
    op = intel.pontuar_oportunidade("p", NOW)
    assert op.componentes["desconto"]["valor"] == pytest.approx(0.2, abs=0.02)   # 10% / 50%
    assert "preço típico" in op.referencia_preco


def test_short_history_falls_back_to_list_price_and_says_it_may_be_inflated():
    product("p")
    seed("p", "L", 27000, 0.1, regular=60000)
    op = intel.pontuar_oportunidade("p", NOW)
    assert op.referencia_preco == "preço cheio informado pela loja"
    assert any("inflado" in n for n in op.notas)


def test_value_per_hour_full_marks_at_5_and_nothing_at_20_reais():
    product("good")
    seed("good", "L", 20000, 0.1)                 # R$ 200 for 40 h = R$ 5/h
    rate("good", playtime=40)
    product("poor")
    seed("poor", "L", 80000, 0.1)                 # R$ 800 for 40 h = R$ 20/h
    rate("poor", playtime=40)
    g = intel.pontuar_oportunidade("good", NOW)
    p = intel.pontuar_oportunidade("poor", NOW)
    assert g.preco_por_hora == 5.0 and g.componentes["valor_hora"]["valor"] == 1.0
    assert p.preco_por_hora == 20.0 and p.componentes["valor_hora"]["valor"] == 0.0


def test_missing_inputs_are_left_out_and_weights_renormalized():
    product("p")
    seed("p", "L", 20000, 0.1)
    rate("p", metacritic=90, priority=0)            # no popularity, no playtime
    op = intel.pontuar_oportunidade("p", NOW)
    assert set(op.componentes) == {"nota", "prioridade"}      # 'desconto' has no reference
    assert 0.34 <= op.cobertura <= 0.36                       # 0.25 + 0.10 of 1.0
    assert any("pouca base" in n for n in op.notas)
    contributions = sum(c["contribuicao"] for c in op.componentes.values())
    assert contributions == pytest.approx(op.score, abs=0.2)  # they add up to the score


def test_wishlist_priority_raises_the_score():
    for pid, pr in (("a", 0), ("b", 2)):
        product(pid)
        seed(pid, "L", 20000, 0.1)
        rate(pid, metacritic=90, popularity=5000, playtime=20, priority=pr)
    a, b = (intel.pontuar_oportunidade(x, NOW).score for x in ("a", "b"))
    assert b > a


def test_better_review_and_popularity_score_higher():
    for pid, mc, pop in (("top", 97, 19000), ("meh", 60, 200)):
        product(pid)
        seed(pid, "L", 20000, 0.1)
        rate(pid, metacritic=mc, popularity=pop)
    assert intel.pontuar_oportunidade("top", NOW).score > intel.pontuar_oportunidade("meh", NOW).score


def test_score_is_bounded_0_to_100():
    product("p")
    history("p", "L", [(d, 60000) for d in range(120, 1, -6)])
    seed("p", "L", 1000, 0.1)
    rate("p", metacritic=100, popularity=10**7, playtime=1000, priority=2)
    assert 0 <= intel.pontuar_oportunidade("p", NOW).score <= 100


# ============================================================ to_cents

@pytest.mark.parametrize("value,cents", [
    (300.1, 30010), ("300,10", 30010), ("0,30", 30), (0.1 + 0.2, 30),
    (0, 0), (150, 15000), ("1234.5", 123450), (0.005, 1), (0.004, 0),
])
def test_money_conversion_has_no_floating_point_drift(value, cents):
    assert intel.to_cents(value) == cents


def test_the_naive_float_conversion_loses_a_cent_and_the_planner_does_not():
    assert int(4.35 * 100) == 434                    # what plain float code does
    assert intel.to_cents(4.35) == 435               # what the planner does


def test_every_amount_up_to_a_thousand_reais_survives_the_round_trip():
    """137 of the first 1999 amounts are truncated by int(reais * 100). Checking
    all of R$ 0,00 to R$ 999,99 proves the conversion is exact."""
    assert sum(1 for c in range(2000) if int(c / 100 * 100) != c) == 137
    assert all(intel.to_cents(c / 100) == c for c in range(100000))


@pytest.mark.parametrize("bad", [-1, "abc", float("nan"), float("inf"), None])
def test_invalid_budgets_are_rejected(bad):
    with pytest.raises(ValueError):
        intel.to_cents(bad)


# ====================================================== knapsack, exact

def test_exact_solver_beats_greedy_where_greedy_is_wrong():
    # A has the best ratio and blocks B and C together, which are worth more.
    items = [(60, 100), (50, 60), (50, 60)]
    assert sorted(intel.melhor_combinacao(items, 100)) == [1, 2]          # value 120
    assert intel._greedy(items, 100) == [0]                               # value 100


def test_ties_on_value_go_to_the_cheaper_subset():
    assert intel.melhor_combinacao([(10, 100), (20, 100)], 20) == [0]


def test_nothing_fits_or_no_value_returns_an_empty_plan():
    assert intel.melhor_combinacao([(50, 10)], 20) == []
    assert intel.melhor_combinacao([(10, 0)], 100) == []
    assert intel.melhor_combinacao([], 100) == []


def _brute(items, budget):
    best = 0
    for r in range(len(items) + 1):
        for combo in itertools.combinations(range(len(items)), r):
            cost = sum(items[i][0] for i in combo)
            if cost <= budget:
                best = max(best, sum(items[i][1] for i in combo))
    return best


def test_solver_matches_brute_force_on_random_instances_and_never_overspends():
    rng = random.Random(20260921)
    for _ in range(300):
        n = rng.randint(0, 9)
        items = [(rng.randint(1, 400), rng.randint(0, 900)) for _ in range(n)]
        budget = rng.randint(0, 1200)
        picks = intel.melhor_combinacao(items, budget)
        cost = sum(items[i][0] for i in picks)
        assert cost <= budget
        assert sum(items[i][1] for i in picks) == _brute(items, budget), (items, budget)


# ========================================================= plan

def wish(pid, price, priority=2, metacritic=None, title=None):
    product(pid, title or pid.upper())
    seed(pid, "L", price, 0.1)
    rate(pid, metacritic=metacritic, priority=priority)
    db.add_watch(pid, "any_drop", None)


def test_plan_never_exceeds_the_budget_and_reports_the_leftover():
    for i, price in enumerate((15000, 25000, 30000, 12000)):
        wish(f"w{i}", price, metacritic=80 + i)
    plan = intel.planejar_orcamento(500, now=NOW, today=TODAY)
    assert plan.gasto_cents <= plan.limite_cents == 50000
    assert plan.sobra_cents == plan.limite_cents - plan.gasto_cents
    assert plan.gasto_cents == sum(i.preco_cents for i in plan.itens)


def test_budget_that_float_arithmetic_would_undershoot_still_fits_both_items():
    """R$ 4,35 buys R$ 2,17 + R$ 2,18. int(4.35 * 100) is 434, which would drop
    one of them and leave a cent 'over budget' that is not."""
    wish("a", 217)
    wish("b", 218)
    plan = intel.planejar_orcamento(4.35, now=NOW, today=TODAY)
    assert plan.limite_cents == 435 and len(plan.itens) == 2 and plan.gasto_cents == 435


def test_exact_plan_is_at_least_as_good_as_greedy():
    for i, price in enumerate((6000, 5000, 5000)):
        wish(f"w{i}", price, priority=2, metacritic=(97 if i == 0 else 80))
    plan = intel.planejar_orcamento(100, now=NOW, today=TODAY)
    assert plan.score_total >= plan.guloso["score_total"]


def test_games_that_should_wait_are_held_back_with_the_reason():
    comparables()
    wish("wait", 27000)
    history("wait", "L", [(d, 30000) for d in (80, 60, 40, 30, 20)])
    seed("wait", "L", 27000, 0.05, regular=30000)          # shallow, Black Friday near
    wish("buy", 20000)
    history("buy", "L", [(d, 40000) for d in (80, 60, 40, 30, 20)])   # usually R$ 400
    plan = intel.planejar_orcamento(1000, ["wait", "buy"], now=NOW, today=dt.date(2026, 11, 5))
    assert [i.product_id for i in plan.itens] == ["buy"]
    assert plan.adiados[0]["product_id"] == "wait" and "Black Friday" in plan.adiados[0]["motivo"]
    everything = intel.planejar_orcamento(1000, ["wait", "buy"], now=NOW,
                                          today=dt.date(2026, 11, 5), respeitar_espere=False)
    assert {i.product_id for i in everything.itens} == {"wait", "buy"}


def test_unpriced_and_too_expensive_items_are_listed_not_dropped_silently():
    product("nope")                                         # no price at all
    wish("dear", 90000)
    plan = intel.planejar_orcamento(100, ["nope", "dear"], now=NOW, today=TODAY)
    assert plan.sem_preco == ["nope"]
    assert plan.nao_cabem[0]["product_id"] == "dear"
    assert plan.itens == []


def test_default_items_are_the_watched_wishlist():
    wish("watched", 10000)
    product("ignored")
    seed("ignored", "L", 10000, 0.1)
    assert [i.product_id for i in intel.planejar_orcamento(500, now=NOW, today=TODAY).itens] == ["watched"]


def test_minimum_score_filters_weak_opportunities():
    wish("weak", 10000, priority=0, metacritic=10)
    wish("strong", 10000, priority=2, metacritic=95)
    plan = intel.planejar_orcamento(1000, now=NOW, today=TODAY, score_minimo=50)
    assert [i.product_id for i in plan.itens] == ["strong"]


def test_plan_is_deterministic():
    for i in range(6):
        wish(f"w{i}", 10000 + 500 * i, metacritic=70 + i)
    a = intel.planejar_orcamento(300, now=NOW, today=TODAY).dict()
    b = intel.planejar_orcamento(300, now=NOW, today=TODAY).dict()
    assert a == b


def test_plan_payload_is_json_ready():
    import json
    wish("a", 10000)
    json.dumps(intel.planejar_orcamento(200, now=NOW, today=TODAY).dict())


# ================================================================ editions

def edition_product(pid, title, price, alias=None, days=0.1, lo=None):
    product(pid, title)
    if alias:
        db.add_alias(pid, "playstation", alias)
    if lo:
        seed(pid, "PS Store", lo, 90)
    seed(pid, "PS Store", price, days)


def test_edition_is_read_from_the_alias_then_from_the_title():
    edition_product("a", "Grand Theft Auto VI", 44990, "10000730#standard")
    edition_product("b", "Grand Theft Auto VI: Ultimate Edition", 54990)
    edition_product("c", "Rise of the Ronin Edição Digital Deluxe", 45590)
    edition_product("d", "Qualquer Jogo", 10000)
    assert [intel.edition_of(x) for x in "abcd"] == ["standard", "ultimate", "deluxe", "standard"]


def test_markup_is_computed_and_the_notice_says_bonus_content_is_not_judged():
    edition_product("std", "Grand Theft Auto VI", 44990, "10000730#standard")
    edition_product("ult", "Grand Theft Auto VI: Ultimate Edition", 54990, "10000730#ultimate")
    out = intel.comprar_edicoes(["ult", "std"], NOW)
    assert [e["edicao"] for e in out["edicoes"]] == ["standard", "ultimate"]
    c = out["comparacoes"][0]
    assert (c["diferenca_cents"], c["diferenca_pct"]) == (10000, 22)
    assert "acréscimo de 22%" in c["veredito"]
    assert "conteúdo bônus não é avaliado" in out["avisos"][0]


def test_premium_edition_that_once_cost_what_standard_costs_today_says_wait():
    edition_product("std", "Elden Ring", 30000, "1#standard")
    edition_product("del", "Elden Ring Deluxe Edition", 45000, "1#deluxe", lo=29000)
    v = intel.comprar_edicoes(["std", "del"], NOW)["comparacoes"][0]["veredito"]
    assert "já custou R$ 290,00" in v and "vale esperar" in v


def test_small_markup_is_called_out():
    edition_product("std", "Jogo", 30000)
    edition_product("del", "Jogo Deluxe", 33000)
    assert "diferença pequena (+10%)" in intel.comprar_edicoes(["std", "del"], NOW)["comparacoes"][0]["veredito"]


def test_premium_cheaper_than_standard_is_flagged_as_an_anomaly():
    edition_product("std", "Jogo", 30000)
    edition_product("del", "Jogo Deluxe", 25000)
    assert "anomalia" in intel.comprar_edicoes(["std", "del"], NOW)["comparacoes"][0]["veredito"]


def test_different_games_are_warned_about():
    edition_product("a", "Grand Theft Auto VI", 44990)
    edition_product("b", "Elden Ring Deluxe Edition", 45000)
    assert any("não parece ser do mesmo jogo" in a for a in intel.comprar_edicoes(["a", "b"], NOW)["avisos"])


def test_needs_two_priced_editions():
    edition_product("a", "Jogo", 30000)
    out = intel.comprar_edicoes(["a", "ghost"], NOW)
    assert out["comparacoes"] == [] and any("pelo menos duas" in a for a in out["avisos"])
    assert any("desconhecido" in a for a in out["avisos"])


# ======================================================= Switch 2 upgrade

def upgrade_case(base=19990, pack=9990, full=24990):
    edition_product("s1", "No Man's Sky", base)
    edition_product("pack", "No Man's Sky – Nintendo Switch 2 Edition Upgrade Pack", pack)
    edition_product("full", "No Man's Sky – Nintendo Switch 2 Edition", full)


def test_base_plus_pack_cheaper_than_the_full_edition():
    upgrade_case(base=10000, pack=5000, full=24990)
    out = intel.comparar_upgrade_switch2("s1", "pack", "full", now=NOW)
    assert out["mais_barato"].startswith("jogo de Switch 1 + Upgrade Pack")
    assert (out["caminhos"][0]["custo_cents"], out["economia_cents"]) == (15000, 9990)


def test_full_edition_cheaper_than_base_plus_pack():
    upgrade_case(base=19990, pack=9990, full=24990)        # 29980 vs 24990
    out = intel.comparar_upgrade_switch2("s1", "pack", "full", now=NOW)
    assert out["mais_barato"] == "edição de Switch 2 completa" and out["economia_cents"] == 4990


def test_owning_the_base_game_only_the_pack_counts():
    upgrade_case(base=19990, pack=9990, full=24990)
    out = intel.comparar_upgrade_switch2("s1", "pack", "full", possui_base=True, now=NOW)
    assert out["caminhos"][0]["custo_cents"] == 9990
    assert out["mais_barato"].startswith("só o Upgrade Pack")


def test_upgrade_comparison_refuses_to_guess_without_a_price():
    upgrade_case()
    db.conn().execute("DELETE FROM price_points WHERE product_id='pack'")
    db.conn().commit()
    out = intel.comparar_upgrade_switch2("s1", "pack", "full", now=NOW)
    assert out["mais_barato"] is None and any("falta preço" in a for a in out["avisos"])


def test_upgrade_comparison_warns_about_a_product_that_is_not_a_pack():
    edition_product("s1", "Zelda", 30000)
    edition_product("notpack", "Zelda Breath of the Wild", 10000)
    edition_product("full", "Zelda – Nintendo Switch 2 Edition", 35000)
    out = intel.comparar_upgrade_switch2("s1", "notpack", "full", now=NOW)
    assert any("não parece ser um Upgrade Pack" in a for a in out["avisos"])


# ============================================================ physical/digital

def signal(pid, sid, title, price, store="Netshoes", days_ago=0.5, now=NOW):
    db.upsert_signal("promobit", sid, pid, title, store, price, None, 0.0, "u", "", 0)
    db.conn().execute("UPDATE deal_signals SET seen_ts=? WHERE source_id=?",
                      (now - int(days_ago * D), sid))
    db.conn().commit()


def test_physical_cheaper_than_digital_is_reported_with_caveats():
    product("p", "Elden Ring")
    seed("p", "PS Store", 25000, 0.1)
    signal("p", "1", "Jogo Elden Ring PS5 Mídia Física", 19000)
    out = intel.comparar_midia("p", NOW)
    assert out["mais_barata"] == "fisica" and out["diferenca_cents"] == 6000
    assert any("frete" in a for a in out["avisos"]) and any("usuário" in a for a in out["avisos"])


def test_digital_cheaper_than_physical():
    product("p", "Elden Ring")
    seed("p", "PS Store", 15000, 0.1)
    signal("p", "1", "Elden Ring PS5 Mídia Física", 19000)
    assert intel.comparar_midia("p", NOW)["mais_barata"] == "digital"


def test_a_code_in_box_is_not_physical_media_and_stale_posts_are_ignored():
    product("p", "Grand Theft Auto VI")
    seed("p", "PS Store", 44990, 0.1)
    signal("p", "1", "Jogo Grand Theft Auto VI (GTA 6) PS5 - Code in Box", 34417)
    signal("p", "2", "Grand Theft Auto VI PS5 Mídia Física", 36000, days_ago=20)   # old post
    out = intel.comparar_midia("p", NOW)
    assert out["fisica"] is None and out["mais_barata"] is None
    assert any("sem preço recente de mídia física" in a for a in out["avisos"])


def test_cheapest_recent_physical_post_wins():
    product("p", "Jogo")
    seed("p", "PS Store", 30000, 0.1)
    signal("p", "1", "Jogo PS5 Mídia Física", 25000)
    signal("p", "2", "Jogo PS5 Mídia Física", 22000, store="KaBuM!")
    assert intel.comparar_midia("p", NOW)["fisica"]["loja"] == "KaBuM!"


def test_the_planner_itself_picks_the_optimal_set_where_greedy_would_not(monkeypatch):
    """Scores are pinned so the case is exact: A is the best value per real but
    blocks B and C, which together are worth more (120 against 100). The solver
    was tested alone; this checks the PLANNER really uses it."""
    prices = {"a": 6000, "b": 5000, "c": 5000}
    scores = {"a": 100.0, "b": 60.0, "c": 60.0}
    for pid in prices:
        wish(pid, prices[pid])

    def fake_score(pid, now=None):
        return intel.Oportunidade(pid, scores[pid], prices[pid], {}, 1.0, "x", None, [])

    monkeypatch.setattr(intel, "pontuar_oportunidade", fake_score)
    plan = intel.planejar_orcamento(100, ["a", "b", "c"], now=NOW, today=TODAY)
    assert {i.product_id for i in plan.itens} == {"b", "c"}
    assert plan.score_total == 120.0 and plan.guloso["score_total"] == 100.0
    assert plan.gasto_cents == 10000
