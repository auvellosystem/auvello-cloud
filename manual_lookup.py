from __future__ import annotations

import argparse
import contextlib
import html
import json
import re
import sys
from urllib.parse import unquote, urlparse

import requests

from app.mercado_livre import MercadoLivreClient
from app.models import Product


_USER_PRODUCT_RE = re.compile(r"\b(MLBU\d{5,})\b", re.I)
_ITEM_RE = re.compile(r"\b(MLB\d{5,})\b", re.I)


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
    products = client.search_products(q=term, limit=5)
    candidates: list[tuple[int, Product]] = []

    for rank, result in enumerate(products):
        product_id = result.get("id")
        if not product_id:
            continue
        # /products/{id}/items só serve para PRODUCT de catálogo. Se a busca
        # retornar um User Product MLBU, ignoramos aqui e seguimos com os demais.
        if str(product_id).upper().startswith("MLBU"):
            continue
        offers = client.product_offers(product_id, discounted_only=False)
        for offer in _top_unique(offers, 2):
            candidates.append((rank, offer))

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


def _clean_title(value: str | None) -> str:
    text = html.unescape(str(value or "")).strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*[|\-–—]\s*Mercado\s+Livre.*$", "", text, flags=re.I)
    text = re.sub(r"\s*[|\-–—]\s*Mercado\s+Libre.*$", "", text, flags=re.I)
    return text.strip()


def _term_from_user_product_api(client: MercadoLivreClient, user_product_id: str) -> str | None:
    try:
        data = client.get_user_product_raw(user_product_id)
    except Exception as exc:
        print(f"[manual-lookup] user-product {user_product_id}: {exc}", file=sys.stderr)
        return None

    candidates = [
        data.get("family_name"),
        data.get("name"),
        data.get("title"),
    ]
    family = data.get("family")
    if isinstance(family, dict):
        candidates.extend([family.get("name"), family.get("family_name")])

    for value in candidates:
        term = _clean_title(value)
        if len(term) >= 4:
            return term
    return None


def _term_from_public_page(reference_url: str) -> str | None:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153 Safari/537.36",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.7",
    }
    try:
        response = requests.get(reference_url, headers=headers, timeout=12, allow_redirects=True)
        response.raise_for_status()
        body = response.text
    except Exception as exc:
        print(f"[manual-lookup] página pública: {exc}", file=sys.stderr)
        return None

    patterns = [
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
        r'"family_name"\s*:\s*"([^"]+)"',
        r'"title"\s*:\s*"([^"]+)"',
        r'<title[^>]*>(.*?)</title>',
    ]
    for pattern in patterns:
        match = re.search(pattern, body, flags=re.I | re.S)
        if not match:
            continue
        raw = match.group(1)
        try:
            raw = bytes(raw, "utf-8").decode("unicode_escape")
        except Exception:
            pass
        term = _clean_title(raw)
        if len(term) >= 4 and "mercado livre" not in term.lower() and "account verification" not in term.lower():
            return term
    return None


def _extract_ids(reference_url: str) -> tuple[str | None, str | None]:
    decoded = unquote(reference_url)
    up = _USER_PRODUCT_RE.search(decoded)
    # item_id do anúncio aparece normalmente em pdp_filters=item_id:MLB...
    item = re.search(r"item_id\s*[:%3A]+\s*(MLB\d{5,})", decoded, flags=re.I)
    if not item:
        all_items = _ITEM_RE.findall(decoded)
        item_id = all_items[-1].upper() if all_items else None
    else:
        item_id = item.group(1).upper()
    return (up.group(1).upper() if up else None, item_id)


def lookup_reference(client: MercadoLivreClient, reference_url: str, fallback_term: str | None, limit: int = 3) -> dict:
    user_product_id, item_id = _extract_ids(reference_url)
    term = None

    if user_product_id:
        term = _term_from_user_product_api(client, user_product_id)

    if not term:
        term = _term_from_public_page(reference_url)

    if not term and fallback_term and not fallback_term.lower().startswith(("http://", "https://")):
        term = fallback_term.strip()

    # Últimos fallbacks: alguns índices aceitam o identificador como busca textual.
    # Isso não é a rota principal, mas evita zerar sem tentar uma resolução adicional.
    fallback_ids = [x for x in (item_id, user_product_id) if x]
    if not term:
        for identifier in fallback_ids:
            probe = lookup_term(client, identifier, limit)
            if probe.get("results"):
                probe.update({
                    "mode": "reference",
                    "reference_url": reference_url,
                    "resolved_term": identifier,
                    "user_product_id": user_product_id,
                    "reference_item_id": item_id,
                })
                return probe

    if not term:
        return {
            "mode": "reference",
            "reference_url": reference_url,
            "resolved_term": None,
            "user_product_id": user_product_id,
            "reference_item_id": item_id,
            "results": [],
            "resolution_error": "Não foi possível identificar o nome do produto a partir deste link MLBU.",
        }

    payload = lookup_term(client, term, limit)
    payload.update({
        "mode": "reference",
        "reference_url": reference_url,
        "resolved_term": term,
        "user_product_id": user_product_id,
        "reference_item_id": item_id,
    })
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Consulta manual de produtos do Auvello")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--product-id")
    group.add_argument("--term")
    group.add_argument("--reference-url")
    parser.add_argument("--fallback-term")
    parser.add_argument("--limit", type=int, default=3)
    args = parser.parse_args()

    limit = max(1, min(args.limit, 3))
    client = MercadoLivreClient()

    with contextlib.redirect_stdout(sys.stderr):
        if args.product_id:
            payload = lookup_product(client, args.product_id.strip().upper(), limit)
        elif args.reference_url:
            payload = lookup_reference(client, args.reference_url.strip(), args.fallback_term, limit)
        else:
            payload = lookup_term(client, args.term.strip(), limit)

    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
