from __future__ import annotations

import argparse
import contextlib
import json
import sys
import re
import unicodedata
import html as html_lib
from urllib.parse import quote, unquote
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


def _product_from_buy_box(result: dict, client: MercadoLivreClient) -> Product | None:
    """Transforma o buy_box_winner do catálogo em uma oferta utilizável.

    /products/search e /products/{id} podem trazer o ganhador mesmo quando
    /products/{id}/items responde `No winners found`. Na consulta instantânea
    não descartamos esse ganhador: ele já é uma publicação comprável.
    """
    product_id = str(result.get("id") or "").strip()
    if not product_id:
        return None

    raw = result
    winner = raw.get("buy_box_winner")
    if not isinstance(winner, dict) or not winner.get("item_id") or winner.get("price") is None:
        try:
            raw = client.get_product_raw(product_id)
        except Exception:
            raw = result
        winner = raw.get("buy_box_winner") if isinstance(raw, dict) else None

    if not isinstance(winner, dict):
        return None
    item_id = str(winner.get("item_id") or "").strip()
    try:
        price = float(winner.get("price"))
    except Exception:
        price = None
    if not item_id or price is None or price <= 0:
        return None

    original_price = winner.get("original_price")
    try:
        original_price = float(original_price) if original_price is not None else None
    except Exception:
        original_price = None

    name = (raw.get("name") or raw.get("family_name") or result.get("name") or result.get("family_name") or product_id)
    pictures = raw.get("pictures") or result.get("pictures") or []
    picture = None
    if pictures and isinstance(pictures[0], dict):
        picture = pictures[0].get("url") or pictures[0].get("secure_url")

    return Product(
        product_id=product_id,
        item_id=item_id,
        name=str(name),
        category_id=winner.get("category_id"),
        domain_id=raw.get("domain_id") or result.get("domain_id"),
        price=price,
        original_price=original_price,
        currency_id=winner.get("currency_id") or "BRL",
        permalink=f"https://www.mercadolivre.com.br/p/{product_id}?wid={item_id}",
        picture=picture,
    )




def _walk_json(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _public_marketplace_search(term: str, limit: int = 20) -> tuple[list[dict], str | None]:
    """Busca anúncios públicos do marketplace sem exigir promoção.

    Tenta dois formatos públicos do Mercado Livre e entende tanto JSON-LD quanto
    os cards atuais (`poly-component__title`) e o layout legado (`ui-search`).
    O fallback é exclusivo da consulta instantânea; a automação dos grupos não
    depende dele.
    """
    normalized = unicodedata.normalize("NFKD", term).encode("ascii", "ignore").decode("ascii")
    slug = "-".join(re.findall(r"[a-z0-9]+", normalized.lower()))
    if not slug:
        return [], "termo vazio após normalização"

    urls = [
        f"https://www.mercadolivre.com.br/jm/search?as_word={quote(term)}",
        f"https://lista.mercadolivre.com.br/{quote(slug, safe='-')}",
    ]
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    rows: list[dict] = []
    seen_urls: set[str] = set()
    errors: list[str] = []

    def add_row(*, title, product_url, price=None, original_price=None, discount=0.0, currency="BRL", picture=None):
        title = html_lib.unescape(re.sub(r"<[^>]+>", " ", str(title or "")))
        title = re.sub(r"\s+", " ", title).strip()
        product_url = html_lib.unescape(str(product_url or "")).replace("&amp;", "&").strip()
        if product_url.startswith("//"):
            product_url = "https:" + product_url
        if not title or not product_url or product_url in seen_urls:
            return
        if "mercadolivre.com.br" not in product_url:
            return
        seen_urls.add(product_url)
        rows.append({
            "name": title,
            "price": price,
            "original_price": original_price,
            "discount_percent": float(discount or 0.0),
            "currency_id": currency or "BRL",
            "origin_url": product_url,
            "picture": picture,
        })

    for url in urls:
        try:
            response = requests.get(url, headers=headers, timeout=settings.request_timeout, allow_redirects=True)
            if response.status_code >= 400:
                errors.append(f"{response.status_code} em {url}")
                continue
            text = response.text
            low = text.lower()
            if "account-verification" in str(response.url).lower() or "account-verification" in low[:5000]:
                errors.append(f"verificação de conta em {url}")
                continue
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            continue

        # 1) JSON-LD: quando a página expõe Product/Offer, é a fonte mais limpa.
        scripts = re.findall(
            r'<script[^>]+type=["\\\']application/ld\+json["\\\'][^>]*>(.*?)</script>',
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
                is_product = "Product" in typ if isinstance(typ, list) else typ == "Product"
                if not is_product:
                    continue
                offers = obj.get("offers") or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                try:
                    price = float(offers.get("price")) if isinstance(offers, dict) and offers.get("price") is not None else None
                except Exception:
                    price = None
                image = obj.get("image")
                if isinstance(image, list):
                    image = image[0] if image else None
                add_row(
                    title=obj.get("name"),
                    product_url=obj.get("url"),
                    price=price,
                    currency=(offers.get("priceCurrency") if isinstance(offers, dict) else None) or "BRL",
                    picture=image if isinstance(image, str) else None,
                )
                if len(rows) >= limit:
                    return rows[:limit], None

        # 2) Cards atuais/legados. Usamos a posição do link no HTML para buscar
        # preço e desconto no mesmo bloco, sem depender de uma classe única.
        anchor_pattern = re.compile(
            r'<a[^>]+(?:class=["\\\'][^"\\\']*(?:poly-component__title|ui-search-link|shops__item-link)[^"\\\']*["\\\'][^>]*)?href=["\\\'](https?://[^"\\\']+mercadolivre\.com\.br/[^"\\\']+)["\\\'][^>]*>(.*?)</a>',
            re.I | re.S,
        )
        for match in anchor_pattern.finditer(text):
            product_url, title_html = match.group(1), match.group(2)
            title = re.sub(r"<[^>]+>", " ", title_html)
            if not title.strip():
                continue
            # Evita links institucionais; anúncios/PDP têm item/catalog id ou slug longo.
            decoded = unquote(html_lib.unescape(product_url))
            if not (re.search(r"/p/MLB\d+", decoded, re.I) or re.search(r"/MLB-?\d+", decoded, re.I) or "/up/MLBU" in decoded.upper()):
                continue
            block = text[match.start(): min(len(text), match.start() + 5000)]
            fractions = re.findall(r'andes-money-amount__fraction[^>]*>\s*([0-9][0-9\.]*)\s*<', block, re.I)
            cents = re.search(r'andes-money-amount__cents[^>]*>\s*([0-9]{1,2})\s*<', block, re.I)
            def amount(raw):
                if not raw:
                    return None
                try:
                    base = float(raw.replace(".", ""))
                    return base + (float(cents.group(1)) / 100 if cents else 0)
                except Exception:
                    return None
            price = amount(fractions[0]) if fractions else None
            original_price = amount(fractions[1]) if len(fractions) > 1 else None
            disc_match = re.search(r'([0-9]{1,2})\s*%\s*OFF', block, re.I)
            discount = float(disc_match.group(1)) if disc_match else 0.0
            image_match = re.search(r'<img[^>]+(?:data-src|src)=["\\\']([^"\\\']+)["\\\']', block, re.I)
            add_row(
                title=title,
                product_url=product_url,
                price=price,
                original_price=original_price,
                discount=discount,
                picture=html_lib.unescape(image_match.group(1)) if image_match else None,
            )
            if len(rows) >= limit:
                return rows[:limit], None

        # 3) JSON embutido do frontend: captura pares permalink/title comuns.
        # É propositalmente conservador para não inventar resultados.
        json_pairs = re.finditer(
            r'"(?:permalink|url)"\s*:\s*"(https:[^"\\]+mercadolivre\.com\.br[^"\\]+)".{0,1200}?"(?:title|name)"\s*:\s*"([^"\\]{4,220})"',
            text,
            re.I | re.S,
        )
        for m in json_pairs:
            product_url = bytes(m.group(1), "utf-8").decode("unicode_escape")
            title = bytes(m.group(2), "utf-8").decode("unicode_escape")
            add_row(title=title, product_url=product_url)
            if len(rows) >= limit:
                return rows[:limit], None

        if rows:
            break

    return rows[:limit], " | ".join(errors) if errors else None


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
        if not offers:
            winner_offer = _product_from_buy_box(result, client)
            if winner_offer is not None:
                offers = [winner_offer]

        for offer in _top_unique(offers, 2):
            effective_coverage, effective_matched = _relevance(term, offer.name or result_name)
            if effective_matched < min_matches:
                continue
            candidates.append((effective_coverage, effective_matched, rank, offer))

    candidates.sort(
        key=lambda pair: (
            -pair[0],
            -pair[1],
            -pair[3].discount_percent,  # promoção é bônus, nunca requisito
            pair[3].price if pair[3].price is not None else float("inf"),
            pair[2],
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
    # IMPORTANTE: consulta instantânea não exige desconto/score mínimo. Esses
    # critérios servem apenas como bônus de ordenação; nunca bloqueiam a resposta.
    # Tentamos as mesmas variações da busca de catálogo para ampliar a cobertura
    # sem abrir a pesquisa para termos genéricos demais.
    public_rows: list[dict] = []
    public_seen: set[str] = set()
    public_errors: list[str] = []
    for public_variant in variants:
        rows, err = _public_marketplace_search(public_variant, limit=20)
        if err:
            public_errors.append(f"{public_variant}: {err}")
        for row in rows:
            key = str(row.get("origin_url") or row.get("name") or "").strip().casefold()
            if not key or key in public_seen:
                continue
            public_seen.add(key)
            public_rows.append(row)
            if len(public_rows) >= 40:
                break
        if len(public_rows) >= 40:
            break
    public_error = " | ".join(public_errors) if public_errors else None
    public_candidates: list[tuple[float, int, float, float, int, dict]] = []
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
        discount = float(row.get("discount_percent") or 0.0)
        # Relevância manda. Dentro de resultados igualmente relevantes, promoção
        # ajuda a subir no ranking, mas 0% de desconto continua elegível.
        public_candidates.append((coverage, matched, discount, price_sort, rank, row))

    public_candidates.sort(
        key=lambda pair: (-pair[0], -pair[1], -pair[2], pair[3], pair[4])
    )
    public_results = [
        _public_result(row, search_rank=rank + 1)
        for _coverage, _matched, _discount, _price, rank, row in public_candidates[:limit]
    ]

    if not public_results:
        print(
            f"[manual-lookup/termo] '{term}': catálogo={len(products)} PRODUCTs sem oferta utilizável; "
            f"marketplace={len(public_rows)} candidato(s); erro={public_error or '-'}",
            file=sys.stderr,
        )

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
