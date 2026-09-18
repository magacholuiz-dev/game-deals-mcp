"""Amazon Brasil — Product Advertising API v5 (assinatura AWS SigV4).

Caminho legitimo unico. Exige conta no Programa de Associados APROVADA e com
vendas qualificadas — ate la a chave nao e emitida e este provider fica inerte
(o resto do sistema continua funcionando normalmente).

Nao ha fallback por raspagem aqui: viola os termos da Amazon e quebra em dias
contra a deteccao de bot. Se precisar cobrir a Amazon antes de ter a PA-API,
o caminho pratico e um servico SERP comercial (Rainforest, Oxylabs, ScraperAPI)
plugado como um provider novo — a interface e a mesma tres funcoes.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json

from ..models import Listing, Offer
from .. import config
from .base import client

HOST = "webservices.amazon.com.br"
REGION = "us-east-1"
SERVICE = "ProductAdvertisingAPI"
MARKETPLACE = "www.amazon.com.br"

RESOURCES = [
    "ItemInfo.Title",
    "Offers.Listings.Price",
    "Offers.Listings.SavingBasis",
    "Offers.Listings.Availability.Message",
    "Offers.Listings.MerchantInfo",
    "Images.Primary.Large",
]


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


class Amazon:
    name = "amazon"
    label = "Amazon Brasil (PA-API v5)"

    def configured(self) -> bool:
        return bool(config.AMAZON_ACCESS_KEY and config.AMAZON_SECRET_KEY
                    and config.AMAZON_PARTNER_TAG)

    def why_unconfigured(self) -> str:
        return ("defina AMAZON_ACCESS_KEY/SECRET_KEY/PARTNER_TAG — exige Associados "
                "aprovado com vendas qualificadas")

    def _request(self, operation: str, payload: dict) -> dict:
        path = f"/paapi5/{operation.lower()}"
        target = f"com.amazon.paapi5.v1.ProductAdvertisingAPIv1.{operation}"
        body = json.dumps(payload, separators=(",", ":"))

        now = dt.datetime.now(dt.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")

        canonical_headers = (
            "content-encoding:amz-1.0\n"
            f"host:{HOST}\n"
            f"x-amz-date:{amz_date}\n"
            f"x-amz-target:{target}\n"
        )
        signed_headers = "content-encoding;host;x-amz-date;x-amz-target"
        payload_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        canonical_request = "\n".join(
            ["POST", path, "", canonical_headers, signed_headers, payload_hash])

        scope = f"{date_stamp}/{REGION}/{SERVICE}/aws4_request"
        to_sign = "\n".join([
            "AWS4-HMAC-SHA256", amz_date, scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()])

        k = _sign(("AWS4" + config.AMAZON_SECRET_KEY).encode("utf-8"), date_stamp)
        k = _sign(k, REGION)
        k = _sign(k, SERVICE)
        k = _sign(k, "aws4_request")
        signature = hmac.new(k, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        headers = {
            "content-encoding": "amz-1.0",
            "content-type": "application/json; charset=utf-8",
            "host": HOST,
            "x-amz-date": amz_date,
            "x-amz-target": target,
            "Authorization": (
                f"AWS4-HMAC-SHA256 Credential={config.AMAZON_ACCESS_KEY}/{scope}, "
                f"SignedHeaders={signed_headers}, Signature={signature}"),
        }
        with client(headers=headers) as c:
            r = c.post(f"https://{HOST}{path}", content=body)
            if r.status_code >= 400:
                return {"_error": r.status_code, "_body": r.text[:400]}
            return r.json()

    def search(self, query: str, limit: int = 10) -> list[Listing]:
        if not self.configured():
            return []
        data = self._request("SearchItems", {
            "Keywords": query, "SearchIndex": "VideoGames",
            "ItemCount": min(limit, 10), "Resources": RESOURCES,
            "PartnerTag": config.AMAZON_PARTNER_TAG, "PartnerType": "Associates",
            "Marketplace": MARKETPLACE,
        })
        items = ((data.get("SearchResult") or {}).get("Items") or [])
        return [self._to_listing(i) for i in items if i.get("ASIN")]

    def fetch(self, source_id: str) -> list[Offer]:
        """source_id = ASIN."""
        if not self.configured():
            return []
        data = self._request("GetItems", {
            "ItemIds": [source_id], "Resources": RESOURCES,
            "PartnerTag": config.AMAZON_PARTNER_TAG, "PartnerType": "Associates",
            "Marketplace": MARKETPLACE,
        })
        items = ((data.get("ItemsResult") or {}).get("Items") or [])
        out: list[Offer] = []
        for i in items:
            listing = ((i.get("Offers") or {}).get("Listings") or [{}])[0]
            price = (listing.get("Price") or {}).get("Amount")
            if price is None:
                continue
            basis = ((listing.get("SavingBasis") or {}).get("Amount"))
            avail = ((listing.get("Availability") or {}).get("Message") or "")
            out.append(Offer(
                source=self.name, source_id=i.get("ASIN", source_id),
                title=(((i.get("ItemInfo") or {}).get("Title") or {})
                       .get("DisplayValue", "")),
                store="Amazon BR",
                price_cents=int(round(float(price) * 100)),
                regular_cents=int(round(float(basis) * 100)) if basis else None,
                currency=(listing.get("Price") or {}).get("Currency", config.CURRENCY),
                url=i.get("DetailPageURL", ""),
                image=((((i.get("Images") or {}).get("Primary") or {})
                        .get("Large") or {}).get("URL", "")),
                in_stock="indispon" not in avail.lower(),
                seller=(((listing.get("MerchantInfo") or {}).get("Name")) or ""),
                extra={"disponibilidade": avail},
            ))
        return out

    @staticmethod
    def _to_listing(i: dict) -> Listing:
        listing = ((i.get("Offers") or {}).get("Listings") or [{}])[0]
        price = (listing.get("Price") or {}).get("Amount")
        return Listing(
            source="amazon", source_id=i.get("ASIN", ""),
            title=(((i.get("ItemInfo") or {}).get("Title") or {}).get("DisplayValue", "")),
            url=i.get("DetailPageURL", ""),
            image=((((i.get("Images") or {}).get("Primary") or {})
                    .get("Large") or {}).get("URL", "")),
            price_cents=int(round(float(price) * 100)) if price else None,
        )


provider = Amazon()
