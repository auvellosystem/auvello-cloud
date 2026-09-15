from __future__ import annotations

import argparse
import contextlib
import json
import sys
import re
import unicodedata
import html as html_lib
from urllib.parse import quote
import requests
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




def _walk_json(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _public_marketplace_search(term: str, limit: int = 12) -> tuple[list[dict], str | None]:
    """Fallback para termos que não existem no catálogo de PRODUCTs.

    A busca pública do Mercado Livre contém também anúncios tradicionais que não
    aparecem em /products/search. Extraímos somente dados visíveis na página
    pública e depois aplicamos o mesmo filtro de relevância do Auvello.
    """
    slug = "-".join(re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", term)
                               .encode("ascii", "ignore").decode("ascii").lower()))
    if not slug:
        return [], "termo vazio após normalização"

    url = f"https://lista.mercadolivre.com.br/{quote(slug, safe='-')}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
        ),
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.7",
    }
    try:
        response = requests.get(url, headers=headers, timeout=settings.request_timeout)
        response.raise_for_status()
    except Exception as exc:
        return [], f"busca pública: {exc}"

    text = response.text
    rows: list[dict] = []
    seen_urls: set[str] = set()

    # 1) JSON-LD quando disponível. É o caminho mais estável e evita depender
    #    de classes CSS do front do Mercado Livre.
    scripts = re.findall(
        r'<script[^>]+type=["\\\']application/ld\\+json["\\\'][^>]*>(.*?)</script>',
        text,
        flags=re.I | re.S,
    )
    for raw in scripts:
        try:
            data = json.loads(html_lib.unescape(raw).strip())
        except Exception:
            continue
        for obj in _walk_json(data):
            typ = obj.get("@type")
            if isinstance(typ, list):
                is_product = "Product" in typ
            else:
                is_product = typ == "Product"
            if not is_product:
                continue
            title = str(obj.get("name") or "").strip()
            product_url = str(obj.get("url") or "").strip()
            image = obj.get("image")
            if isinstance(image, list):
                image = image[0] if image else None
            offers = obj.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            price = None
            if isinstance(offers, dict):
                try:
                    price = float(offers.get("price")) if offers.get("price") is not None else None
                except Exception:
                    price = None
            if not title or not product_url or product_url in seen_urls:
                continue
            seen_urls.add(product_url)
            rows.append({
                "name": title,
                "price": price,
                "original_price": None,
                "discount_percent": 0.0,
                "currency_id": (offers.get("priceCurrency") if isinstance(offers, dict) else None) or "BRL",
                "origin_url": product_url,
                "picture": image if isinstance(image, str) else None,
            })
            if len(rows) >= limit:
                break
        if len(rows) >= limit:
            break

    # 2) Fallback leve para páginas sem JSON-LD de Product. Captura links de
    #    anúncios e deriva o título do slug. Preço pode ficar ausente; nesses
    #    casos o resultado ainda pode ser exibido e o link afiliado continua válido.
    if not rows:
        hrefs = re.findall(r'href=["\\\'](https://[^"\\\']+mercadolivre\\.com\\.br/[^"\\\']+)["\\\']', text, flags=re.I)
        for product_url in hrefs:
            product_url = html_lib.unescape(product_url)
            if product_url in seen_urls:
                continue
            if not (re.search(r'/p/MLB\\d+', product_url, re.I) or re.search(r'/MLB-?\\d+', product_url, re.I) or '/up/MLBU' in product_url.upper()):
                continue
            path = product_url.split('?', 1)[0].rstrip('/').split('/')[-1]
            path = re.sub(r'^(MLB-?\\d+-)', '', path, flags=re.I)
            path = re.sub(r'(_JM|MLBU\\d+)$', '', path, flags=re.I)
            title = re.sub(r'[-_]+', ' ', path).strip().title()
            if not title:
                continue
            seen_urls.add(product_url)
            rows.append({
                "name": title,
                "price": None,
                "original_price": None,
                "discount_percent": 0.0,
                "currency_id": "BRL",
                "origin_url": product_url,
                "picture": None,
            })
            if len(rows) >= limit:
                break

    return rows, None


def _public_result(row: dict, *, search_rank: int | None = None) -> dict:
    origin_url = str(row.get("origin_url") or "").strip()
    return {
        "product_id": None,
        "item_id": None,
        "name": row.get("name"),
        "price": row.get("price"),
        "original_price": row.get("original_price"),
        "discount_percent": float(row.get("discount_percent") or 0.0),
        "currency_id": row.get("currency_id") or "BRL",
        "url": _affiliate_url(origin_url) if origin_url else None,
        "picture": row.get("picture"),
        "search_rank": search_rank,
    }

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




_GENERIC_SEARCH_WORDS = {
    "jogo", "kit", "conjunto", "peca", "pecas", "unidade", "unidades",
    "novo", "nova", "original", "produto", "modelo",
}


def _search_variants(term: str, max_variants: int = 3) -> list[str]:
    """Gera poucas variações mais tolerantes sem abrir demais a busca.

    Ex.: "Jogo de Tapete Fusca" ->
         ["Jogo de Tapete Fusca", "tapete fusca"]
    """
    raw = " ".join(str(term or "").split()).strip()
    if not raw:
        return []

    variants: list[str] = []

    def add(value: str) -> None:
        value = " ".join(str(value or "").split()).strip()
        if not value:
            return
        key = value.casefold()
        if key not in {v.casefold() for v in variants}:
            variants.append(value)

    add(raw)

    normalized = unicodedata.normalize("NFKD", raw)
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch)).lower()
    ordered_tokens = [
        w for w in re.findall(r"[a-z0-9]+", normalized)
        if len(w) >= 2 and w not in _STOPWORDS
    ]
    important = [w for w in ordered_tokens if w not in _GENERIC_SEARCH_WORDS]
    if len(important) >= 2:
        add(" ".join(important))

    # Mantém as palavras mais específicas no final como último fallback.
    # Isso ajuda consultas como "jogo tapete automotivo fusca" sem pesquisar
    # termos isolados genéricos.
    if len(important) >= 3:
        add(" ".join(important[-3:]))

    return variants[:max_variants]

def _relevance(query: str, name: str) -> tuple[float, int]:
    query_tokens = _tokens(query)
    name_tokens = _tokens(name)
    if not query_tokens or not name_tokens:
        return 0.0, 0
    matched = len(query_tokens & name_tokens)
    return matched / len(query_tokens), matched


def lookup_term(client: MercadoLivreClient, term: str, limit: int = 3) -> dict:
    # /products/search pode ser bem mais rígido que a busca pública do Mercado
    # Livre. Fazemos poucas tentativas controladas com o mesmo produto descrito
    # de forma mais enxuta e depois unificamos os PRODUCTs encontrados.
    variants = _search_variants(term)
    product_rows: dict[str, tuple[int, dict]] = {}
    search_attempts: list[dict] = []

    for variant_index, variant in enumerate(variants):
        try:
            rows = client.search_products(q=variant, limit=12)
        except Exception as exc:
            print(f"[manual-lookup/termo] busca '{variant}': {exc}", file=sys.stderr)
            search_attempts.append({"query": variant, "count": 0, "error": str(exc)})
            continue

        search_attempts.append({"query": variant, "count": len(rows)})
        for rank, result in enumerate(rows):
            product_id = result.get("id")
            if not product_id:
                continue
            # Prioriza a posição da primeira variação que encontrou o PRODUCT.
            combined_rank = variant_index * 100 + rank
            current = product_rows.get(product_id)
            if current is None or combined_rank < current[0]:
                product_rows[product_id] = (combined_rank, result)

    products = [pair[1] for pair in sorted(product_rows.values(), key=lambda x: x[0])]
    candidates: list[tuple[float, int, int, Product]] = []
    query_token_count = len(_tokens(term))

    for rank, result in enumerate(products):
        product_id = result.get("id")
        if not product_id:
            continue
        result_status = str(result.get("status") or "active").lower()
        if result_status != "active":
            continue

        result_name = result.get("name") or result.get("family_name") or ""
        coverage, matched = _relevance(term, result_name)

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
            effective_coverage, effective_matched = _relevance(term, offer.name or result_name)
            if effective_matched < min_matches:
                continue
            candidates.append((effective_coverage, effective_matched, rank, offer))

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

    api_results = [_result(product, search_rank=rank + 1) for rank, product in chosen]
    if api_results:
        return {
            "mode": "term",
            "query": term,
            "source": "catalog_api",
            "search_variants": variants,
            "search_attempts": search_attempts,
            "searched_products": len(products),
            "results": api_results,
        }

    # Nada útil no catálogo: procura também nos anúncios públicos do marketplace.
    public_rows, public_error = _public_marketplace_search(term, limit=20)
    public_candidates: list[tuple[float, int, float, int, dict]] = []
    query_token_count = len(_tokens(term))
    if query_token_count <= 2:
        public_min_matches, public_min_coverage = 1, 0.50
    elif query_token_count <= 4:
        public_min_matches, public_min_coverage = 2, 0.40
    else:
        public_min_matches, public_min_coverage = 3, 0.30

    for rank, row in enumerate(public_rows):
        coverage, matched = _relevance(term, str(row.get("name") or ""))
        if matched < public_min_matches or coverage < public_min_coverage:
            continue
        price = row.get("price")
        price_sort = float(price) if price is not None else float("inf")
        public_candidates.append((coverage, matched, price_sort, rank, row))

    public_candidates.sort(key=lambda pair: (-pair[0], -pair[1], pair[2], pair[3]))
    public_results = [
        _public_result(row, search_rank=rank + 1)
        for _coverage, _matched, _price, rank, row in public_candidates[:limit]
    ]

    return {
        "mode": "term",
        "query": term,
        "source": "public_marketplace" if public_results else "none",
        "search_variants": variants,
        "search_attempts": search_attempts,
        "searched_products": len(products),
        "public_candidates": len(public_rows),
        "public_error": public_error,
        "results": public_results,
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
