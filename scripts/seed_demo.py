"""Popula um banco de DEMONSTRACAO com produtos e precos reais, mais um
historico sintetico, so para voce ver o dashboard cheio antes de ter semanas de
coleta. Escreve em demo.db — nunca toca no seu deals.db.

    GAMEDEALS_DB=./demo.db uv run python scripts/seed_demo.py
    GAMEDEALS_DB=./demo.db uv run game-deals-web
"""
import os
import random

os.environ.setdefault("GAMEDEALS_DB", "./demo.db")

from game_deals import db, providers          # noqa: E402
from game_deals.collector import refresh_product  # noqa: E402

DAY = 86400
random.seed(7)

# Foco em Switch 2 e PS5. Os ids sao os que a loja BR realmente precifica:
# slug pt-br para Switch 2, NSUID das Americas para Switch 1, concept id para PS5.
CATALOGO = [
    ("nintendo",    "mario-kart-world-switch-2",  "Mario Kart World",          "switch2"),
    ("nintendo",    "donkey-kong-bananza-switch-2", "Donkey Kong Bananza",     "switch2"),
    ("nintendo",    "70010000000025", "Zelda: Breath of the Wild",             "switch"),
    ("nintendo",    "70010000012332", "Super Mario Odyssey",                   "switch"),
    ("playstation", "10000493",       "Mozart Requiem",                        "ps5"),
    ("steam",       "3240220",        "Grand Theft Auto V Enhanced",           "pc"),
]

# (dias_atras, fator sobre o preco atual). Fatores < 1 = ja esteve mais barato.
PERFIS = {
    "historico": [(400, 1.00), (300, .92), (200, .78), (120, .95), (60, 1.02),
                  (30, 1.00), (14, 1.00)],          # hoje sera o menor de todos
    "destaque":  [(400, .55), (300, .98), (210, 1.00), (120, 1.05), (60, 1.02),
                  (25, 1.04), (10, 1.03)],          # so foi menor ha >1 ano
    "bom":       [(300, .80), (180, 1.02), (90, 1.05), (45, 1.03), (20, 1.01)],
    "caro":      [(200, .60), (90, .72), (40, .78), (12, .85)],  # hoje esta caro
}
ATRIBUICAO = ["destaque", "historico", "bom", "caro", "destaque", "historico"]


def main() -> None:
    print(f"banco: {os.environ['GAMEDEALS_DB']}\n")
    for (src, sid, titulo, plat), perfil in zip(CATALOGO, ATRIBUICAO):
        prov = providers.get(src)
        try:
            listings = prov.search(titulo.split("–")[0].strip(), 6)
        except Exception:
            listings = []
        imagem = next((l.image for l in listings
                       if l.source_id == sid and l.image), "")
        if not imagem and listings:
            imagem = listings[0].image

        compat = ""
        if src == "nintendo":
            from game_deals.providers.nintendo import compatibilidade, NATIVO, RETRO
            compat = NATIVO if plat == "switch2" else RETRO

        pid = titulo.lower().replace(" ", "-").replace(":", "")[:40]
        db.upsert_product(pid, titulo, "game", plat, imagem, compat)
        db.add_alias(pid, src, sid)

        ofertas = refresh_product(pid, quiet=True)
        if not ofertas:
            print(f"  ! {titulo}: sem preco na regiao, pulando")
            continue
        atual = ofertas[0].price_cents
        loja = ofertas[0].store or src

        for dias, fator in PERFIS[perfil]:
            ruido = random.uniform(.985, 1.015)
            db.record(pid, src, loja, int(atual * fator * ruido),
                      ofertas[0].regular_cents, "BRL", True, ofertas[0].url,
                      ts=db.now() - dias * DAY)

        print(f"  {titulo:36} {atual/100:>8.2f}  perfil={perfil}"
              f"  img={'sim' if imagem else 'NAO'}")

    print(f"\n{len(db.list_products())} produtos. Rode: "
          f"GAMEDEALS_DB=./demo.db uv run game-deals-web")


if __name__ == "__main__":
    main()
