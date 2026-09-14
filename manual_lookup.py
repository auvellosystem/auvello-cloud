from __future__ import annotations

import argparse
import contextlib
import json
import sys
from dataclasses import asdict

from app.mercado_livre import MercadoLivreClient
from app.models import Product


def _result(product: Product, *, search_rank: int | None = None) -> dict:
    url = f"https://www.mercadolivre.com.br/p/{product.product_id}"
    if product.item_id:
        url += f"?wid={product.item_id}"
    return {
        "product_id": product.product_id,
        "item_id": product.item_id,
        "name": product.name,
        "price": product.price,
        "original_price": product.original_price,
        "discount_percent": product.discount_percent,
        "currency_id": product.currency_id,
        "url": url,
        "picture": product.picture,
        "search_rank": search_rank,
    }


def _top_unique(offers: list[Product], limit: int = 3) -> list[Product]:
    unique: dict[str, Product] = {}
    for offer in offers:
        key = offer.item_id or f"{offer.product_id}:{offer.price}"
        current = unique.get(key)
        if current is None or (offer.price or float("inf")) < (current.price or float("inf")):
            unique[key] = offer
    ranked = sorted(
        unique.values(),
        key=lambda p: (
            p.price if p.price is not None else float("inf"),
            -p.discount_percent,
        ),
    )
    return ranked[:limit]


def lookup_product(client: MercadoLivreClient, product_id: str, limit: int = 3) -> dict:
    offers = client.product_offers(product_id, discounted_only=False)
    top = _top_unique(offers, limit)
    return {
        "mode": "product",
        "query": product_id,
        "total_offers": len(offers),
        "results": [_result(p) for p in top],
    }


def lookup_term(client: MercadoLivreClient, term: str, limit: int = 3) -> dict:
    # O /products/search já devolve PRODUCTs por relevância. Consultamos somente
    # os primeiros PRODUCTs e, dentro desse conjunto relevante, priorizamos preço.
    products = client.search_products(q=term, limit=5)
    candidates: list[tuple[int, Product]] = []

    for rank, result in enumerate(products):
        product_id = result.get("id")
        if not product_id:
            continue
        offers = client.product_offers(product_id, discounted_only=False)
        # Um anúncio muito caro do mesmo PRODUCT não precisa ocupar os 3 resultados.
        # Guardamos as duas melhores ofertas de cada PRODUCT antes da comparação final.
        for offer in _top_unique(offers, 2):
            candidates.append((rank, offer))

    # "Mais barato e relevante": preço decide entre os PRODUCTs que o próprio
    # Mercado Livre considerou mais relevantes na busca; rank resolve empates.
    candidates.sort(
        key=lambda pair: (
            pair[1].price if pair[1].price is not None else float("inf"),
            pair[0],
            -pair[1].discount_percent,
        )
    )

    chosen: list[tuple[int, Product]] = []
    seen_items: set[str] = set()
    for rank, offer in candidates:
        key = offer.item_id or f"{offer.product_id}:{offer.price}"
        if key in seen_items:
            continue
        seen_items.add(key)
        chosen.append((rank, offer))
        if len(chosen) >= limit:
            break

    return {
        "mode": "term",
        "query": term,
        "searched_products": len(products),
        "results": [_result(product, search_rank=rank + 1) for rank, product in chosen],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Consulta manual de produtos do Auvello")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--product-id")
    group.add_argument("--term")
    parser.add_argument("--limit", type=int, default=3)
    args = parser.parse_args()

    limit = max(1, min(args.limit, 3))
    client = MercadoLivreClient()

    # Clientes internos podem registrar diagnósticos em stdout. Para o Node receber
    # JSON limpo, esses logs vão para stderr e somente o resultado sai em stdout.
    with contextlib.redirect_stdout(sys.stderr):
        if args.product_id:
            payload = lookup_product(client, args.product_id.strip().upper(), limit)
        else:
            payload = lookup_term(client, args.term.strip(), limit)

    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
