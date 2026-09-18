from __future__ import annotations

from typing import Protocol, runtime_checkable

import httpx

from .. import config
from ..models import Listing, Offer


@runtime_checkable
class Provider(Protocol):
    name: str
    label: str

    def configured(self) -> bool: ...
    def why_unconfigured(self) -> str: ...
    def search(self, query: str, limit: int = 10) -> list[Listing]: ...
    def fetch(self, source_id: str) -> list[Offer]: ...


def client(**kw) -> httpx.Client:
    headers = {"User-Agent": config.USER_AGENT, "Accept": "application/json"}
    headers.update(kw.pop("headers", {}))
    return httpx.Client(timeout=config.HTTP_TIMEOUT, headers=headers,
                        follow_redirects=True, **kw)


def cents(value) -> int | None:
    """Aceita float/str em reais e devolve centavos. Tolera None e lixo."""
    if value is None:
        return None
    try:
        return int(round(float(value) * 100))
    except (TypeError, ValueError):
        return None
