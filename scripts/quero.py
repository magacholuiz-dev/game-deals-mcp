"""Wishlist pela linha de comando.

    # títulos exatos
    uv run python scripts/quero.py "GTA VI" "Ghost of Yotei" "Marvel's Wolverine"

    # franquias (os melhores de cada, no Switch 2)
    uv run python scripts/quero.py --franquia Zelda Mario --plataforma switch2

    # os melhores do Switch 1 que você perdeu (rodam no Switch 2)
    uv run python scripts/quero.py --classicos 15
"""
from __future__ import annotations

import argparse
import os
import sys

os.environ.setdefault("GAMEDEALS_DB", "./deals.db")

from game_deals import db, ratings, wishlist   # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="adiciona jogos à wishlist")
    ap.add_argument("titulos", nargs="*", help="títulos exatos")
    ap.add_argument("--franquia", nargs="+", default=[],
                    help="termos de franquia (Zelda, Mario)")
    ap.add_argument("--classicos", type=int, metavar="N",
                    help="N melhores do Switch 1 (rodam no Switch 2)")
    ap.add_argument("--plataforma", default="",
                    help="switch2 | switch | ps5 | pc")
    ap.add_argument("--por-franquia", type=int, default=4)
    ap.add_argument("--nota-minima", type=int, default=0)
    args = ap.parse_args()

    if not ratings.configured():
        sys.exit("Falta RAWG_API_KEY no .env — sem ela não há nota nem busca.")
    if not (args.titulos or args.franquia or args.classicos):
        ap.print_help()
        return

    if args.titulos:
        print("TÍTULOS")
        for t in args.titulos:
            r = wishlist.adiciona(t, args.plataforma)
            print(wishlist.formata(r) if r else f"  ! não encontrei: {t}")

    for termo in args.franquia:
        plat = args.plataforma or "switch"
        print(f"\nFRANQUIA {termo.upper()} ({plat})")
        achados = wishlist.franquia(termo, plat, args.por_franquia,
                                    args.nota_minima)
        for r in achados:
            print(wishlist.formata(r))
        if not achados:
            print("  ! nada encontrado")

    if args.classicos:
        print(f"\nCLÁSSICOS DO SWITCH 1 — rodam no Switch 2 "
              f"(nota mínima {max(args.nota_minima, 85)})")
        for r in wishlist.classicos_switch1(args.classicos,
                                            max(args.nota_minima, 85)):
            print(wishlist.formata(r))

    print(f"\n{len(db.list_products())} produtos, "
          f"{len(db.active_watches())} vigiados. Veja: ./scripts/web.sh")


if __name__ == "__main__":
    main()
