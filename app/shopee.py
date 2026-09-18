from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import requests

from .config import settings
from .models import Product


PRODUCT_OFFER_QUERY = """
query ProductOffers($keyword: String!, $page: Int!, $limit: Int!) {
  productOfferV2(
    keyword: $keyword
    page: $page
    limit: $limit
    listType: 0
    sortType: 2
  ) {
    nodes {
      itemId
      productName
      productLink
      offerLink
      imageUrl
      priceMin
      priceMax
      priceDiscountRate
      sales
      ratingStar
      shopName
    }
    pageInfo { page limit hasNextPage }
  }
}
""".strip()


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class ShopeeClient:
    """Cliente da API oficial de Afiliados da Shopee Brasil."""

    def __init__(self) -> None:
        self.session = requests.Session()

    @property
    def configured(self) -> bool:
        return bool(
            settings.shopee_enabled
            and settings.shopee_app_id
            and settings.shopee_secret
            and settings.shopee_api_url
        )

    def _post(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        # A assinatura precisa usar exatamente o mesmo JSON enviado no corpo.
        body = json.dumps(
            {"query": query, "variables": variables},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        timestamp = int(time.time())
        payload = f"{settings.shopee_app_id}{timestamp}{body}{settings.shopee_secret}"
        signature = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        authorization = (
            f"SHA256 Credential={settings.shopee_app_id}, "
            f"Timestamp={timestamp}, Signature={signature}"
        )

        response = self.session.post(
            settings.shopee_api_url,
            data=body.encode("utf-8"),
            headers={
                "Authorization": authorization,
                "Content-Type": "application/json",
            },
            timeout=settings.request_timeout,
        )
        if not response.ok:
            raise RuntimeError(
                f"Shopee HTTP {response.status_code}: {response.text[:500]}"
            )

        data = response.json()
        errors = data.get("errors") or []
        if errors:
            messages = "; ".join(str(error.get("message") or error) for error in errors)
            raise RuntimeError(f"Shopee GraphQL: {messages}")
        return data.get("data") or {}

    def search_offers(self, keyword: str, limit: int | None = None) -> list[Product]:
        if not self.configured:
            return []

        requested = max(1, min(int(limit or settings.shopee_products_per_term), 50))
        data = self._post(
            PRODUCT_OFFER_QUERY,
            {"keyword": keyword, "page": 1, "limit": requested},
        )
        container = data.get("productOfferV2") or {}
        nodes = container.get("nodes") or []

        offers: list[Product] = []
        seen: set[str] = set()
        for node in nodes:
            item_id = str(node.get("itemId") or "").strip()
            name = str(node.get("productName") or "").strip()
            affiliate_url = str(node.get("offerLink") or "").strip()
            price = _number(node.get("priceMin"))
            discount = _number(node.get("priceDiscountRate")) or 0.0

            # Sem offerLink não há garantia de rastreamento/comissão.
            if not item_id or item_id in seen or not name or not affiliate_url:
                continue
            if price is None or price <= 0:
                continue

            # A API informa preço e percentual. Reconstruímos o preço anterior
            # somente quando o desconto é válido para o cálculo das regras.
            original_price = None
            if 0 < discount < 100:
                original_price = round(price / (1 - discount / 100), 2)

            key = f"shopee:{item_id}"
            offers.append(
                Product(
                    product_id=key,
                    item_id=key,
                    name=name,
                    category_id=None,
                    domain_id=None,
                    price=price,
                    original_price=original_price,
                    currency_id="BRL",
                    permalink=affiliate_url,
                    picture=str(node.get("imageUrl") or "").strip() or None,
                    discovery_source="shopee",
                    marketplace="shopee",
                )
            )
            seen.add(item_id)

        return offers
