"""Mercado Livre — API oficial.

Crie um app gratuito em developers.mercadolivre.com.br/devcenter e coloque
ML_CLIENT_ID / ML_CLIENT_SECRET no .env. Usamos client_credentials; se a sua
aplicacao exigir token de usuario para /sites/MLB/search, troque o
`_token()` por um refresh_token guardado — o resto do codigo nao muda.
"""
from __future__ import annotations

import time

from ..models import Listing, Offer
from .. import config
from .base import cents, client

API = "https://api.mercadolibre.com"
SITE = "MLB"  # Brasil

_token_cache: dict[str, float | str] = {"value": "", "exp": 0.0}


def salva_refresh_token(token: str) -> None:
    """Reescreve ML_REFRESH_TOKEN no .env, preservando o resto do arquivo."""
    from pathlib import Path
    env = Path(__file__).resolve().parents[3] / ".env"
    linhas = []
    if env.exists():
        linhas = [l for l in env.read_text().splitlines()
                  if not l.startswith("ML_REFRESH_TOKEN=")]
    linhas.append(f"ML_REFRESH_TOKEN={token}")
    env.write_text("\n".join(linhas) + "\n")
    env.chmod(0o600)
    config.ML_REFRESH_TOKEN = token


class MercadoLivre:
    name = "mercadolivre"
    label = "Mercado Livre (BR)"

    def configured(self) -> bool:
        return bool(config.ML_CLIENT_ID and config.ML_CLIENT_SECRET)

    def why_unconfigured(self) -> str:
        return ("defina ML_CLIENT_ID e ML_CLIENT_SECRET (app gratuito no DevCenter) "
                "e rode: uv run python scripts/ml_auth.py")

    def _token(self) -> str:
        if _token_cache["value"] and time.time() < float(_token_cache["exp"]):
            return str(_token_cache["value"])

        tentativas = []
        if config.ML_REFRESH_TOKEN:
            tentativas.append({"grant_type": "refresh_token",
                               "client_id": config.ML_CLIENT_ID,
                               "client_secret": config.ML_CLIENT_SECRET,
                               "refresh_token": config.ML_REFRESH_TOKEN})
        tentativas.append({"grant_type": "client_credentials",
                           "client_id": config.ML_CLIENT_ID,
                           "client_secret": config.ML_CLIENT_SECRET})

        for dados in tentativas:
            try:
                with client() as c:
                    r = c.post(f"{API}/oauth/token", data=dados, headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Accept": "application/json"})
                if r.status_code >= 400:
                    continue
                j = r.json()
            except Exception:
                continue

            token = j.get("access_token", "")
            if not token:
                continue
            _token_cache["value"] = token
            _token_cache["exp"] = time.time() + float(j.get("expires_in", 21600)) - 120
            # O ML rotaciona: guarde o novo ou a proxima renovacao falha.
            novo = j.get("refresh_token")
            if novo and novo != config.ML_REFRESH_TOKEN:
                salva_refresh_token(novo)
            return token

        return ""

    def _auth(self) -> dict[str, str]:
        t = self._token()
        return {"Authorization": f"Bearer {t}"} if t else {}

    def search(self, query: str, limit: int = 10) -> list[Listing]:
        if not self.configured():
            return []
        with client(headers=self._auth()) as c:
            r = c.get(f"{API}/sites/{SITE}/search",
                      params={"q": query, "limit": limit, "condition": "new"})
            if r.status_code >= 400:
                return []
            results = (r.json() or {}).get("results", [])
        return [
            Listing(source=self.name, source_id=i.get("id", ""),
                    title=i.get("title", ""),
                    url=i.get("permalink", ""),
                    image=(i.get("thumbnail") or "").replace("http://", "https://"),
                    price_cents=cents(i.get("price")),
                    extra={"vendedor": (i.get("seller") or {}).get("nickname"),
                           "frete_gratis": (i.get("shipping") or {}).get("free_shipping"),
                           "condicao": i.get("condition")})
            for i in results if i.get("id")
        ]

    def fetch(self, source_id: str) -> list[Offer]:
        """source_id = MLB123456789."""
        if not self.configured():
            return []
        with client(headers=self._auth()) as c:
            r = c.get(f"{API}/items/{source_id}")
            if r.status_code >= 400:
                return []
            i = r.json() or {}
        price_c = cents(i.get("price"))
        if price_c is None:
            return []
        available = int(i.get("available_quantity") or 0)
        return [Offer(
            source=self.name, source_id=source_id,
            title=i.get("title", ""), store="Mercado Livre",
            price_cents=price_c,
            regular_cents=cents(i.get("original_price")),
            currency=i.get("currency_id", config.CURRENCY),
            url=i.get("permalink", ""),
            # pictures[0] e alta resolucao; thumbnail e o fallback pequeno
            image=(((i.get("pictures") or [{}])[0].get("secure_url"))
                   or (i.get("thumbnail") or "").replace("http://", "https://")),
            in_stock=i.get("status") == "active" and available > 0,
            seller=str((i.get("seller_id") or "")),
            extra={"estoque": available, "condicao": i.get("condition")},
        )]


provider = MercadoLivre()
