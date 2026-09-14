from __future__ import annotations

import requests

from .auth import get_valid_access_token
from .config import settings
from .models import Product

API = "https://api.mercadolibre.com"


def _number(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class MercadoLivreClient:
    """
    PRODUCT é a ponte para as OFERTAS reais.

    Fonte principal:
        /products/{PRODUCT_ID}/items

    buy_box_winner é apenas atalho/fallback, não requisito.
    ITEM/USER_PRODUCT diretos de Highlights não são consultados.
    """

    def __init__(self) -> None:
        self.session = requests.Session()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {get_valid_access_token()}"}

    def _get(self, path: str, params: dict | None = None):
        response = self.session.get(
            f"{API}{path}",
            headers=self._headers(),
            params=params,
            timeout=settings.request_timeout,
        )
        if not response.ok:
            try:
                detail = response.json()
            except Exception:
                detail = response.text
            raise RuntimeError(
                f"Mercado Livre HTTP {response.status_code}: {detail}"
            )
        return response.json()

    def trends(self) -> list[dict]:
        data = self._get(f"/trends/{settings.site_id}")
        return data if isinstance(data, list) else []

    def highlights(self, category_id: str) -> list[dict]:
        data = self._get(
            f"/highlights/{settings.site_id}/category/{category_id}"
        )
        return data.get("content") or []

    def get_category(self, category_id: str) -> dict:
        return self._get(f"/categories/{category_id}")

    def search_products(
        self,
        q: str | None = None,
        product_identifier: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        params = {
            "site_id": settings.site_id,
            "status": "active",
            "limit": limit or settings.max_products_per_query,
        }
        if product_identifier:
            params["product_identifier"] = product_identifier
        elif q:
            params["q"] = q
        else:
            return []

        data = self._get("/products/search", params=params)
        return data.get("results") or []

    def get_product_raw(self, product_id: str) -> dict:
        return self._get(f"/products/{product_id}")

    def get_user_product_raw(self, user_product_id: str) -> dict:
        """Obtém metadados de um User Product (MLBU...) quando a API permitir.

        Esse recurso é diferente de /products/{PRODUCT_ID}. Alguns User Products
        de terceiros podem não estar disponíveis para o token da aplicação; por
        isso o chamador deve tratar falhas como fallback, não como erro fatal.
        """
        return self._get(f"/user-products/{user_product_id}")

    def product_offers(
        self,
        product_id: str,
        *,
        discounted_only: bool = False,
        min_discount: int = 1,
    ) -> list[Product]:
        """
        Obtém as publicações/ofertas associadas ao PRODUCT.

        discounted_only=True usa o filtro oficial discount=X-100.
        """
        params = {}
        if discounted_only:
            params["discount"] = f"{max(1, int(min_discount))}-100"

        try:
            data = self._get(
                f"/products/{product_id}/items",
                params=params,
            )
        except Exception as exc:
            print(f"[product-items] {product_id}: {exc}")
            return []

        results = data.get("results") or []
        if not results:
            return []

        # Metadados do PRODUCT dão nome, domínio e imagem.
        try:
            product_raw = self.get_product_raw(product_id)
        except Exception:
            product_raw = {}

        name = (
            product_raw.get("name")
            or product_raw.get("family_name")
            or product_id
        )
        domain_id = product_raw.get("domain_id")

        picture = None
        pictures = product_raw.get("pictures") or []
        if pictures and isinstance(pictures[0], dict):
            picture = pictures[0].get("url")

        offers: list[Product] = []

        for item in results:
            price = _number(item.get("price"))
            item_id = item.get("item_id")
            category_id = item.get("category_id")

            if not item_id or price is None or price <= 0:
                continue

            # A URL de catálogo é estável e o gerador de afiliado já foi
            # validado com /p/PRODUCT_ID.
            permalink = (
                f"https://www.mercadolivre.com.br/p/{product_id}"
            )

            offers.append(
                Product(
                    product_id=product_id,
                    item_id=item_id,
                    name=name,
                    category_id=category_id,
                    domain_id=domain_id,
                    price=price,
                    original_price=_number(item.get("original_price")),
                    currency_id=item.get("currency_id") or "BRL",
                    permalink=permalink,
                    picture=picture,
                )
            )

        return offers

    def offers_from_search_result(
        self,
        result: dict,
        *,
        discounted_only: bool = False,
        min_discount: int = 1,
    ) -> list[Product]:
        product_id = result.get("id")
        if not product_id:
            return []
        return self.product_offers(
            product_id,
            discounted_only=discounted_only,
            min_discount=min_discount,
        )
