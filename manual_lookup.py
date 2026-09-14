from __future__ import annotations

import argparse
import contextlib
import json
import sys
import re
import unicodedata
from dataclasses import asdict

from app.mercado_livre import MercadoLivreClient
from app.models import Product
from app.affiliate import AffiliateClient, AffiliateError
from app.config import settings


def _affiliate_url(origin_url: str) -> str | None:
    # Na consulta manual, nunca exibimos um link cru como se fosse link do Auvello.
    # Se o modo de afiliado estiver desativado ou o portal falhar, o resultado
    # continua visível, mas sem botão de compra.
    if settings.affiliate_mode == "disabled":
        return None
    try:
        return AffiliateClient().build(origin_url)
    except AffiliateError as exc:
        print(f"[manual-lookup/afiliado] {exc}", file=sys.stderr)
        return None


def _result(product: Product, *, search_rank: int | None = None) -> dict:
    origin_url = f"https://www.mercadolivre.com.br/p/{product.product_id}"
    if product.item_id:
        origin_url += f"?wid={product.item_id}"
    return {
        "product_id": product.product_id,
        "item_id": product.item_id,
        "name": product.name,
        "price": product.price,
        "original_price": product.original_price,
        "discount_percent": product.discount_percent,
        "currency_id": product.currency_id,
        "url": _affiliate_url(origin_url),
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


_STOPWORDS = {
    "a", "o", "as", "os", "de", "da", "do", "das", "dos", "e", "em",
    "para", "por", "com", "sem", "um", "uma", "no", "na", "nos", "nas",
}


def _tokens(value: str) -> set[str]:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    words = re.findall(r"[a-z0-9]+", text)
    return {w for w in words if len(w) >= 2 and w not in _STOPWORDS}


def _relevance(query: str, name: str) -> tuple[float, int]:
    query_tokens = _tokens(query)
    name_tokens = _tokens(name)
    if not query_tokens or not name_tokens:
        return 0.0, 0
    matched = len(query_tokens & name_tokens)
    return matched / len(query_tokens), matched


def lookup_term(client: MercadoLivreClient, term: str, limit: int = 3) -> dict:
    # O Mercado Livre entrega PRODUCTs em ordem de relevância. Além disso,
    # calculamos aderência textual para impedir que um item barato, porém só
    # vagamente relacionado (ex.: um livro), passe na frente do produto pedido.
    products = client.search_products(q=term, limit=8)
    candidates: list[tuple[float, int, int, Product]] = []
    query_token_count = len(_tokens(term))

    for rank, result in enumerate(products):
        product_id = result.get("id")
        if not product_id:
            continue
        # O search já é chamado com status=active, mas validamos novamente para
        # evitar produto de catálogo inativo em qualquer resposta inconsistente.
        result_status = str(result.get("status") or "active").lower()
        if result_status != "active":
            continue

        result_name = result.get("name") or result.get("family_name") or ""
        coverage, matched = _relevance(term, result_name)

        # Busca longa precisa de aderência mais forte. Isso evita resultados que
        # coincidem apenas com palavras genéricas como "luz" ou "leitura".
        if query_token_count <= 2:
            min_matches, min_coverage = 1, 0.50
        elif query_token_count <= 4:
            min_matches, min_coverage = 2, 0.40
        else:
            min_matches, min_coverage = 3, 0.30
        if matched < min_matches or coverage < min_coverage:
            continue

        offers = client.product_offers(product_id, discounted_only=False)
        for offer in _top_unique(offers, 2):
            # Usa o nome efetivo da oferta/produto, quando disponível, para
            # recalcular a relevância antes do ranking final.
            effective_coverage, effective_matched = _relevance(term, offer.name or result_name)
            if effective_matched < min_matches:
                continue
            candidates.append((effective_coverage, effective_matched, rank, offer))

    # Relevância vem antes do preço. Entre opções igualmente aderentes ao pedido,
    # escolhemos a mais barata e usamos a ordem do Mercado Livre como desempate.
    candidates.sort(
        key=lambda pair: (
            -pair[0],
            -pair[1],
            pair[3].price if pair[3].price is not None else float("inf"),
            pair[2],
            -pair[3].discount_percent,
        )
    )

    chosen: list[tuple[int, Product]] = []
    seen_items: set[str] = set()
    for _coverage, _matched, rank, offer in candidates:
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
