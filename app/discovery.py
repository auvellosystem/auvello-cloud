from __future__ import annotations

import json
from pathlib import Path
from collections import Counter

from .mercado_livre import MercadoLivreClient
from .models import Product
from .config import settings


AUVELLO_ROOTS = {
    "eletronicos_tecnologia": ["MLB1000", "MLB1648"],
    "moda_vestuario": ["MLB1430"],
    "celulares_acessorios": ["MLB1051"],
    "games_acessorios": ["MLB1144"],
    "utilidades_domesticas": ["MLB1574", "MLB5726"],
    "pet_shop": ["MLB1071"],
}


class Discovery:
    """
    OFERTA é a entidade principal.

    Highlights / Watchlist / Trends -> PRODUCT
    PRODUCT -> /products/{id}/items -> ofertas reais

    Não depende de buy_box_winner.
    Não depende de pai/filho.
    """

    def __init__(self, ml: MercadoLivreClient) -> None:
        self.ml = ml

    def run(self) -> list[Product]:
        offers: dict[str, Product] = {}

        if getattr(settings, "discovery_highlights", True):
            self._from_highlights(offers)

        if getattr(settings, "discovery_watchlist", True):
            self._from_watchlist(offers)

        if getattr(settings, "discovery_trends", True):
            self._from_trends(offers)

        print(f"[discovery] {len(offers)} ofertas únicas encontradas")
        return list(offers.values())

    def _add(self, out: dict[str, Product], offer: Product) -> bool:
        if not offer:
            return False
        if offer.price is None or offer.price <= 0:
            return False
        if not offer.item_id:
            return False
        if not offer.permalink:
            return False

        out[str(offer.item_id)] = offer
        return True

    def _add_many(
        self,
        out: dict[str, Product],
        offers: list[Product],
    ) -> int:
        added = 0
        for offer in offers:
            before = len(out)
            self._add(out, offer)
            if len(out) > before:
                added += 1
        return added

    def _from_highlights(self, out: dict[str, Product]) -> None:
        """
        Highlights serve só para descobrir PRODUCTs relevantes.
        Para reduzir chamadas, buscamos ofertas já com desconto mínimo.
        """
        max_children_categories = 4
        min_discount = max(
            1,
            int(getattr(settings, "min_discount_percent", 15)),
        )

        for group_name, roots in AUVELLO_ROOTS.items():
            print(f"[highlights] grupo={group_name}")

            for root_id in roots:
                try:
                    category = self.ml.get_category(root_id)
                except Exception as exc:
                    print(f"[highlights] categoria {root_id}: {exc}")
                    continue

                children = category.get("children_categories") or []
                category_ids = [
                    c.get("id")
                    for c in children[:max_children_categories]
                    if c.get("id")
                ] or [root_id]

                for category_id in category_ids:
                    try:
                        entries = self.ml.highlights(category_id)
                    except Exception as exc:
                        print(
                            f"[highlights] {category_id}: ignorado ({exc})"
                        )
                        continue

                    counts = Counter(
                        (e.get("type") or "UNKNOWN").upper()
                        for e in entries
                    )
                    product_ids = [
                        e.get("id")
                        for e in entries
                        if (e.get("type") or "").upper() == "PRODUCT"
                        and e.get("id")
                    ]

                    found = 0
                    for product_id in product_ids:
                        offers = self.ml.product_offers(
                            product_id,
                            discounted_only=True,
                            min_discount=min_discount,
                        )
                        found += self._add_many(out, offers)

                    print(
                        f"[highlights] {group_name} / {category_id}: "
                        f"PRODUCT={len(product_ids)} | "
                        f"ITEM ignorado={counts.get('ITEM', 0)} | "
                        f"USER_PRODUCT ignorado={counts.get('USER_PRODUCT', 0)} | "
                        f"ofertas >= {min_discount}%={found}"
                    )

    def _from_watchlist(self, out: dict[str, Product]) -> None:
        """
        Watchlist é intencionalmente mais ampla:
        pega todas as ofertas dos PRODUCTs e deixa service/rules decidir.
        Isso permite também detectar queda histórica mesmo sem original_price.
        """
        path = Path("watchlist.json")
        if not path.exists():
            print("[watchlist] watchlist.json não encontrado")
            return

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[watchlist] erro lendo watchlist.json: {exc}")
            return

        for q in data.get("queries", []):
            try:
                results = self.ml.search_products(q=q)
                found = 0

                for result in results:
                    offers = self.ml.offers_from_search_result(result)
                    found += self._add_many(out, offers)

                print(
                    f"[watchlist] {q!r}: "
                    f"{len(results)} PRODUCTs, "
                    f"{found} ofertas"
                )
            except Exception as exc:
                print(f"[watchlist] {q!r}: {exc}")

        for code in data.get("product_identifiers", []):
            try:
                results = self.ml.search_products(
                    product_identifier=code
                )
                found = 0
                for result in results:
                    found += self._add_many(
                        out,
                        self.ml.offers_from_search_result(result),
                    )
                print(
                    f"[watchlist/id] {code!r}: "
                    f"{len(results)} PRODUCTs, {found} ofertas"
                )
            except Exception as exc:
                print(f"[watchlist/id] {code!r}: {exc}")

    def _from_trends(self, out: dict[str, Product]) -> None:
        """
        Trends também usa desconto mínimo para não explodir a quantidade
        de ofertas e chamadas durante a descoberta automática.
        """
        try:
            trends = self.ml.trends()
            print(f"[trends] {len(trends)} tendências recebidas")
        except Exception as exc:
            print(f"[trends] erro: {exc}")
            return

        max_terms = min(
            int(getattr(settings, "max_trend_terms", 20)),
            10,
        )
        min_discount = max(
            1,
            int(getattr(settings, "min_discount_percent", 15)),
        )

        found = 0
        for entry in trends[:max_terms]:
            q = entry.get("keyword")
            if not q:
                continue

            try:
                results = self.ml.search_products(q=q)
                for result in results:
                    offers = self.ml.offers_from_search_result(
                        result,
                        discounted_only=True,
                        min_discount=min_discount,
                    )
                    found += self._add_many(out, offers)
            except Exception as exc:
                print(f"[trend-query] {q!r}: {exc}")

        print(
            f"[trends] {max_terms} termos testados, "
            f"{found} ofertas >= {min_discount}%"
        )
