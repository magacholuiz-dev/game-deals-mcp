"""Regras de alerta e envio da notificacao."""
from __future__ import annotations

import subprocess

from . import config, db
from .models import Offer, brl
from .verdict import Verdict

RULES = {
    "price_below": "preço abaixo de R$ X",
    "discount_above": "desconto acima de X%",
    "new_low": "menor preco ja registrado",
    "any_drop": "qualquer queda em relação à última leitura",
    "back_in_stock": "voltou ao estoque (pré-venda / edição especial)",
}


def evaluate(rule_kind: str, rule_value: float | None, offer: Offer,
             verdict: Verdict, previous: dict | None) -> str | None:
    """Devolve a manchete do alerta, ou None se a regra nao disparou."""
    if rule_kind == "price_below":
        if rule_value and offer.price_cents <= rule_value * 100:
            return f"{brl(offer.price_cents)} — abaixo do seu alvo de {brl(int(rule_value * 100))}"

    elif rule_kind == "discount_above":
        if rule_value and offer.cut_pct >= rule_value:
            return f"{offer.cut_pct}% OFF — {brl(offer.price_cents)}"

    elif rule_kind == "new_low":
        # "menor preço já registrado" com 2 leituras de 1 dia é tecnicamente
        # verdade e inútil — dispararia em todo produto novo, no primeiro dia.
        # Mesma guarda do selo: sem histórico confiável, não é notícia.
        if verdict.is_all_time_low and verdict.enough_history:
            return f"{brl(offer.price_cents)} — {verdict.label}"

    elif rule_kind == "any_drop":
        if previous and offer.price_cents < previous["price_cents"]:
            delta = previous["price_cents"] - offer.price_cents
            return (f"caiu {brl(delta)} — de {brl(previous['price_cents'])} "
                    f"para {brl(offer.price_cents)}")

    elif rule_kind == "back_in_stock":
        was_out = previous is not None and not previous.get("in_stock", 1)
        if offer.in_stock and (was_out or previous is None):
            return f"disponível — {brl(offer.price_cents)}"

    return None


def notify(title: str, message: str, url: str = "") -> list[str]:
    """Envia para os canais configurados. Retorna quais canais funcionaram."""
    sent: list[str] = []

    if config.NTFY_TOPIC:
        try:
            import httpx
            headers = {"Title": title.encode("utf-8").decode("latin-1", "ignore"),
                       "Tags": "video_game"}
            if url:
                headers["Click"] = url
            httpx.post(f"https://ntfy.sh/{config.NTFY_TOPIC}",
                       data=message.encode("utf-8"), headers=headers, timeout=10)
            sent.append("ntfy")
        except Exception:
            pass

    if config.MACOS_NOTIFY:
        try:
            subprocess.run(
                ["terminal-notifier", "-title", title, "-message", message,
                 *(["-open", url] if url else [])],
                check=True, capture_output=True, timeout=10)
            sent.append("macos")
        except Exception:
            pass

    return sent
