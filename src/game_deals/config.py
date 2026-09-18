"""Configuracao lida de .env / ambiente. Nada aqui levanta excecao: um provider
sem credencial simplesmente se declara nao-configurado e e ignorado."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")


def _s(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


DB_PATH = _s("GAMEDEALS_DB", "./deals.db")
COUNTRY = _s("COUNTRY", "BR")
CURRENCY = _s("CURRENCY", "BRL")

ITAD_API_KEY = _s("ITAD_API_KEY")

ML_CLIENT_ID = _s("ML_CLIENT_ID")
ML_CLIENT_SECRET = _s("ML_CLIENT_SECRET")
# Obtido uma vez por scripts/ml_auth.py. O fluxo principal do ML e
# authorization_code; client_credentials nao e documentado para busca.
ML_REFRESH_TOKEN = _s("ML_REFRESH_TOKEN")
# O ML recusa http:// na validacao do DevCenter — precisa ser https.
ML_REDIRECT_URI = _s("ML_REDIRECT_URI", "https://localhost:8788/callback")

AMAZON_ACCESS_KEY = _s("AMAZON_ACCESS_KEY")
AMAZON_SECRET_KEY = _s("AMAZON_SECRET_KEY")
AMAZON_PARTNER_TAG = _s("AMAZON_PARTNER_TAG")

SHOPEE_APP_ID = _s("SHOPEE_APP_ID")
SHOPEE_APP_SECRET = _s("SHOPEE_APP_SECRET")

NTFY_TOPIC = _s("NTFY_TOPIC")
MACOS_NOTIFY = _s("MACOS_NOTIFY", "0") == "1"

HTTP_TIMEOUT = 20.0
USER_AGENT = "game-deals-mcp/0.1 (+uso pessoal)"
