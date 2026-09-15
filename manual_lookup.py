from __future__ import annotations

import argparse
import os
import contextlib
import json
import sys
import re
import unicodedata
import html as html_lib
from urllib.parse import quote, unquote, urlparse, parse_qs
import requests
from dataclasses import asdict

from app.mercado_livre import MercadoLivreClient
from app.models import Product
from app.affiliate import AffiliateClient, AffiliateError
from app.config import settings


def _canonical_affiliate_candidates(
    origin_url: str,
    *,
    product_id: str | None = None,
    item_id: str | None = None,
) -> list[str]:
    """Monta URLs canônicas do Mercado Livre para o portal de afiliados.

    A consulta manual pode descobrir a mesma oferta por catálogo, User Product,
    anúncio comum ou resultado externo. Nenhum parâmetro de tracking (?wid,
    matt_*, gclid etc.) é enviado ao gerador de afiliados.
    """
    raw = str(origin_url or "").strip()
    candidates: list[str] = []

    def add(url: str | None) -> None:
        value = str(url or "").strip()
        if not value or value in candidates:
            return
        try:
            parsed = urlparse(value)
            host = (parsed.hostname or "").lower()
            if host != "mercadolivre.com.br" and not host.endswith(".mercadolivre.com.br"):
                return
        except Exception:
            return
        candidates.append(value)

    pid = str(product_id or "").strip().upper()
    iid = str(item_id or "").strip().upper()

    # PRODUCT de catálogo: é exatamente o mesmo formato usado pela automação.
    if re.fullmatch(r"MLB\d+", pid):
        add(f"https://www.mercadolivre.com.br/p/{pid}")

    # Aproveita identificadores expostos pelo próprio URL descoberto.
    mlbu = None
    url_item = None
    if raw:
        mlbu_match = re.search(r"\b(MLBU\d{5,})\b", raw, re.I)
        if mlbu_match:
            mlbu = mlbu_match.group(1).upper()
        item_match = re.search(r"\b(MLB\d{5,})\b", raw, re.I)
        if item_match:
            url_item = item_match.group(1).upper()
        try:
            parsed = urlparse(raw)
            params = parse_qs(parsed.query)
            filters = " ".join(params.get("pdp_filters", []))
            fm = re.search(r"item_id\s*[:=]\s*(MLB\d+)", unquote(filters), re.I)
            if fm:
                url_item = fm.group(1).upper()
            fragment = unquote(parsed.fragment or "")
            wm = re.search(r"(?:^|[&?])wid=(MLB\d+)", fragment, re.I)
            if wm:
                url_item = wm.group(1).upper()
        except Exception:
            pass

    if not iid:
        iid = url_item or ""

    # User Product: usa o caminho canônico sem querystring/fragmento.
    if mlbu:
        add(f"https://www.mercadolivre.com.br/up/{mlbu}")

    # Para anúncios comuns/URLs com slug, preserva somente host + path.
    if raw:
        try:
            parsed = urlparse(raw)
            host = (parsed.hostname or "").lower()
            if host == "mercadolivre.com.br" or host.endswith(".mercadolivre.com.br"):
                clean = f"https://{parsed.netloc}{parsed.path}".rstrip("/")
                add(clean)
        except Exception:
            pass

    # Último formato seguro para um item quando ele é tudo o que temos.
    # Não adiciona tracking; o Mercado Livre pode redirecionar para a página atual.
    if re.fullmatch(r"MLB\d+", iid):
        digits = iid[3:]
        add(f"https://produto.mercadolivre.com.br/MLB-{digits}-_JM")

    return candidates


def _affiliate_url(
    origin_url: str,
    *,
    product_id: str | None = None,
    item_id: str | None = None,
) -> str | None:
    # Na consulta manual, nunca exibimos link cru como se fosse afiliado.
    if settings.affiliate_mode == "disabled":
        return None

    candidates = _canonical_affiliate_candidates(
        origin_url,
        product_id=product_id,
        item_id=item_id,
    )
    if not candidates:
        print(f"[manual-lookup/afiliado] nenhuma URL canônica para {origin_url}", file=sys.stderr)
        return None

    client = AffiliateClient()
    errors: list[str] = []
    for candidate in candidates:
        try:
            short_url = client.build(candidate)
            print(f"[manual-lookup/afiliado] OK origem={candidate}", file=sys.stderr)
            return short_url
        except AffiliateError as exc:
            errors.append(f"{candidate} -> {exc}")

    print(
        "[manual-lookup/afiliado] todas as URLs foram rejeitadas: " + " | ".join(errors),
        file=sys.stderr,
    )
    return None


def _result(product: Product, *, search_rank: int | None = None) -> dict:
    origin_url = f"https://www.mercadolivre.com.br/p/{product.product_id}"
    return {
        "product_id": product.product_id,
        "item_id": product.item_id,
        "name": product.name,
        "price": product.price,
        "original_price": product.original_price,
        "discount_percent": product.discount_percent,
        "currency_id": product.currency_id,
        "url": _affiliate_url(
            origin_url,
            product_id=product.product_id,
            item_id=product.item_id,
        ),
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
        permalink=f"https://www.mercadolivre.com.br/p/{product_id}",
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



def _parse_brl_price(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    # Aceita formatos como R$ 1.299,90, 1299.90 e 1,299.90.
    cleaned = re.sub(r"[^0-9,\.]", "", text)
    if not cleaned:
        return None
    try:
        if "," in cleaned and "." in cleaned:
            if cleaned.rfind(",") > cleaned.rfind("."):
                cleaned = cleaned.replace(".", "").replace(",", ".")
            else:
                cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            parts = cleaned.split(",")
            if len(parts[-1]) in (1, 2):
                cleaned = "".join(parts[:-1]).replace(".", "") + "." + parts[-1]
            else:
                cleaned = cleaned.replace(",", "")
        elif cleaned.count(".") > 1:
            parts = cleaned.split(".")
            if len(parts[-1]) in (1, 2):
                cleaned = "".join(parts[:-1]) + "." + parts[-1]
            else:
                cleaned = "".join(parts)
        return float(cleaned)
    except Exception:
        return None


def _serper_marketplace_search(term: str, limit: int = 20) -> tuple[list[dict], str | None]:
    """Descobre anúncios do Mercado Livre via Serper Shopping.

    Esse fallback existe porque o HTML de busca do Mercado Livre pode redirecionar
    datacenters (como Render) para verificação de conta. A Serper devolve resultados
    estruturados do Google Shopping; filtramos estritamente por Mercado Livre e
    usamos apenas como descoberta. O link final ainda passa pelo AffiliateClient.
    """
    api_key = (os.getenv("SERPER_API_KEY") or "").strip()
    if not api_key:
        return [], "SERPER_API_KEY não configurada"

    headers = {
        "X-API-KEY": api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "q": term,
        "gl": "br",
        "hl": "pt-br",
        "num": max(10, min(limit * 3, 30)),
    }
    try:
        response = requests.post(
            "https://google.serper.dev/shopping",
            headers=headers,
            json=payload,
            timeout=settings.request_timeout,
        )
        if response.status_code >= 400:
            body = response.text[:400].replace("\n", " ")
            return [], f"Serper HTTP {response.status_code}: {body}"
        data = response.json()
    except Exception as exc:
        return [], f"Serper: {exc}"

    rows: list[dict] = []
    seen: set[str] = set()
    for entry in data.get("shopping") or []:
        link = str(entry.get("link") or "").strip()
        source = str(entry.get("source") or "").lower()
        low = link.lower()
        if "mercadolivre.com.br" not in low and "mercado livre" not in source:
            continue
        if not link or link in seen:
            continue
        seen.add(link)
        price = _parse_brl_price(entry.get("price"))
        old_price = _parse_brl_price(entry.get("oldPrice") or entry.get("old_price"))
        discount = 0.0
        if price and old_price and old_price > price:
            discount = max(0.0, (old_price - price) / old_price * 100.0)
        rows.append({
            "name": entry.get("title") or entry.get("name"),
            "price": price,
            "original_price": old_price,
            "discount_percent": discount,
            "currency_id": "BRL",
            "origin_url": link,
            "picture": entry.get("imageUrl") or entry.get("image"),
            "external_position": entry.get("position"),
        })
        if len(rows) >= limit:
            break

    # Alguns produtos podem não aparecer no vertical Shopping. Como segunda
    # tentativa estruturada, usa pesquisa web e mantém somente URLs do ML.
    if not rows:
        web_payload = {
            "q": f'site:mercadolivre.com.br {term}',
            "gl": "br",
            "hl": "pt-br",
            "num": max(10, min(limit * 3, 30)),
        }
        try:
            response = requests.post(
                "https://google.serper.dev/search",
                headers=headers,
                json=web_payload,
                timeout=settings.request_timeout,
            )
            if response.status_code < 400:
                data = response.json()
                for entry in data.get("organic") or []:
                    link = str(entry.get("link") or "").strip()
                    if "mercadolivre.com.br" not in link.lower() or link in seen:
                        continue
                    seen.add(link)
                    rows.append({
                        "name": entry.get("title"),
                        "price": None,
                        "original_price": None,
                        "discount_percent": 0.0,
                        "currency_id": "BRL",
                        "origin_url": link,
                        "picture": (
                            entry.get("imageUrl")
                            or entry.get("image")
                            or entry.get("thumbnail")
                        ),
                        "external_position": entry.get("position"),
                    })
                    if len(rows) >= limit:
                        break
        except Exception:
            pass

    return rows[:limit], None if rows else "Serper não retornou anúncios do Mercado Livre"

def _public_result(row: dict, *, search_rank: int | None = None) -> dict:
    origin_url = str(row.get("origin_url") or "").strip()
    mlbu, item_id = _extract_reference_ids(origin_url) if origin_url else (None, None)
    product_id = None
    if origin_url:
        pm = re.search(r"/p/(MLB\d+)", origin_url, re.I)
        if pm:
            product_id = pm.group(1).upper()
    return {
        "product_id": product_id,
        "item_id": item_id,
        "name": row.get("name"),
        "price": row.get("price"),
        "original_price": row.get("original_price"),
        "discount_percent": float(row.get("discount_percent") or 0.0),
        "currency_id": row.get("currency_id") or "BRL",
        "url": _affiliate_url(
            origin_url,
            product_id=product_id,
            item_id=item_id,
        ) if origin_url else None,
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

    # Nada útil no catálogo: usa uma busca externa estruturada para descobrir
    # anúncios do marketplace sem depender do HTML do Mercado Livre (que pode
    # bloquear datacenters com verificação de conta). Critérios promocionais
    # NÃO bloqueiam a consulta instantânea.
    serper_rows: list[dict] = []
    serper_seen: set[str] = set()
    serper_errors: list[str] = []
    for variant in variants:
        rows, err = _serper_marketplace_search(variant, limit=20)
        if err:
            serper_errors.append(f"{variant}: {err}")
        for row in rows:
            key = str(row.get("origin_url") or row.get("name") or "").strip().casefold()
            if not key or key in serper_seen:
                continue
            serper_seen.add(key)
            serper_rows.append(row)
            if len(serper_rows) >= 40:
                break
        if len(serper_rows) >= 40:
            break

    if serper_rows:
        ext_candidates: list[tuple[float, int, float, float, int, dict]] = []
        query_token_count = len(_tokens(term))
        if query_token_count <= 2:
            ext_min_matches, ext_min_coverage = 1, 0.50
        elif query_token_count <= 4:
            ext_min_matches, ext_min_coverage = 2, 0.40
        else:
            ext_min_matches, ext_min_coverage = 3, 0.30

        for rank, row in enumerate(serper_rows):
            coverage, matched = _relevance(term, str(row.get("name") or ""))
            if matched < ext_min_matches or coverage < ext_min_coverage:
                continue
            price = row.get("price")
            price_sort = float(price) if price is not None else float("inf")
            discount = float(row.get("discount_percent") or 0.0)
            ext_candidates.append((coverage, matched, discount, price_sort, rank, row))

        # Relevância primeiro; desconto e preço apenas refinam os melhores
        # resultados. 0% OFF nunca elimina um anúncio relevante.
        ext_candidates.sort(
            key=lambda pair: (-pair[0], -pair[1], -pair[2], pair[3], pair[4])
        )
        ext_results = [
            _public_result(row, search_rank=rank + 1)
            for _coverage, _matched, _discount, _price, rank, row in ext_candidates[:limit]
        ]
        if ext_results:
            return {
                "mode": "term",
                "query": term,
                "source": "serper_marketplace",
                "search_variants": variants,
                "search_attempts": search_attempts,
                "searched_products": len(products),
                "external_candidates": len(serper_rows),
                "external_errors": serper_errors,
                "results": ext_results,
            }

    # Último fallback: tenta o HTML público do marketplace. Em alguns hosts isso
    # pode ser bloqueado por verificação de conta, então ele não é mais o caminho
    # principal.
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
            f"serper={len(serper_rows)} candidato(s); marketplace={len(public_rows)} candidato(s); "
            f"erro_serper={' | '.join(serper_errors) or '-'}; erro_marketplace={public_error or '-'}",
            file=sys.stderr,
        )

    return {
        "mode": "term",
        "query": term,
        "source": "public_marketplace" if public_results else "none",
        "search_variants": variants,
        "search_attempts": search_attempts,
        "searched_products": len(products),
        "external_candidates": len(serper_rows),
        "external_errors": serper_errors,
        "public_candidates": len(public_rows),
        "public_error": public_error,
        "results": public_results,
    }



def _extract_reference_ids(reference_url: str) -> tuple[str | None, str | None]:
    raw = str(reference_url or "").strip()
    mlbu = None
    item_id = None
    m = re.search(r"\b(MLBU\d{5,})\b", raw, re.I)
    if m:
        mlbu = m.group(1).upper()
    m = re.search(r"\b(MLB\d{5,})\b", raw, re.I)
    if m:
        item_id = m.group(1).upper()
    try:
        u = urlparse(raw)
        params = parse_qs(u.query)
        filters = " ".join(params.get("pdp_filters", []))
        fm = re.search(r"item_id\s*[:=]\s*(MLB\d+)", unquote(filters), re.I)
        if fm:
            item_id = fm.group(1).upper()
        # Alguns links compartilhados deixam wid/item no fragmento.
        fragment = unquote(u.fragment or "")
        wm = re.search(r"(?:^|[&?])wid=(MLB\d+)", fragment, re.I)
        if wm:
            item_id = wm.group(1).upper()
    except Exception:
        pass
    return mlbu, item_id


def _term_from_reference_slug(reference_url: str) -> str:
    try:
        u = urlparse(str(reference_url or "").strip())
        parts = [p for p in u.path.split("/") if p]
        stop = next((i for i,p in enumerate(parts) if p.lower() in {"up", "p"}), -1)
        candidates = parts[:stop] if stop > 0 else []
        if not candidates:
            return ""
        slug = " ".join(candidates)
        slug = re.sub(r"[-_]+", " ", slug)
        slug = re.sub(r"\bMLBU?\d+\b", " ", slug, flags=re.I)
        slug = re.sub(r"\s+", " ", slug).strip()
        return slug[:180]
    except Exception:
        return ""


def _clean_reference_title(value: str) -> str:
    title = html_lib.unescape(str(value or ""))
    title = re.sub(r"<[^>]+>", " ", title)
    title = re.sub(r"\s*[|\-–—]\s*(?:Mercado\s*Livre|MercadoLibre).*?$", "", title, flags=re.I)
    title = re.sub(r"\s+", " ", title).strip()
    if title.lower() in {"mercado livre brasil", "mercado livre"}:
        return ""
    return title[:180]


def _resolve_reference_via_http(reference_url: str) -> tuple[str, str | None]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Accept-Language": "pt-BR,pt;q=0.9",
    }
    try:
        r = requests.get(reference_url, headers=headers, timeout=settings.request_timeout, allow_redirects=True)
        if r.status_code >= 400:
            return "", f"HTTP {r.status_code}"
        low = (r.text or "").lower()
        if "account-verification" in str(r.url).lower() or "account-verification" in low[:8000]:
            return "", "verificação de conta"
        # Se o redirecionamento trouxe um slug rico, ele é a melhor fonte.
        slug_term = _term_from_reference_slug(str(r.url))
        if slug_term:
            return slug_term, None
        for pattern in [
            r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\']([^"\']+)',
            r'<title[^>]*>(.*?)</title>',
        ]:
            m = re.search(pattern, r.text or "", re.I | re.S)
            if m:
                title = _clean_reference_title(m.group(1))
                if title:
                    return title, None
        return "", "título não encontrado"
    except Exception as exc:
        return "", str(exc)


def _resolve_reference_via_serper(reference_url: str, mlbu: str | None, item_id: str | None) -> tuple[str, dict | None, str | None]:
    api_key = (os.getenv("SERPER_API_KEY") or "").strip()
    if not api_key:
        return "", None, "SERPER_API_KEY não configurada"
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    queries = []
    if item_id:
        queries.append(f'"{item_id}"')
    if mlbu:
        queries.append(f'"{mlbu}"')
    queries.append(reference_url)
    errors=[]
    for q in queries:
        payload={"q": f"site:mercadolivre.com.br {q}", "gl":"br", "hl":"pt-br", "num":10}
        try:
            r=requests.post("https://google.serper.dev/search",headers=headers,json=payload,timeout=settings.request_timeout)
            if r.status_code>=400:
                errors.append(f"HTTP {r.status_code}")
                continue
            data=r.json()
            for entry in data.get("organic") or []:
                link=str(entry.get("link") or "")
                if "mercadolivre.com.br" not in link.lower():
                    continue
                title=_clean_reference_title(entry.get("title") or "")
                if not title:
                    continue
                # Exige o identificador quando o resultado o expõe na URL/snippet,
                # evitando resolver um produto totalmente diferente.
                hay=(link+" "+str(entry.get("snippet") or "")+" "+str(entry.get("title") or "")).upper()
                if item_id and item_id not in hay and mlbu and mlbu not in hay:
                    # Para a busca pelo URL completo, o título ainda pode ser válido.
                    if q != reference_url:
                        continue
                return title, {
                    "origin_url": link,
                    "name": title,
                    "picture": (
                        entry.get("imageUrl")
                        or entry.get("image")
                        or entry.get("thumbnail")
                    ),
                }, None
        except Exception as exc:
            errors.append(str(exc))
    return "", None, " | ".join(errors) if errors else "referência não localizada"



def _catalog_display_metadata(client: MercadoLivreClient, product_id: str) -> tuple[str | None, str | None]:
    """Obtém nome e primeira imagem do catálogo oficial para exibição no WhatsApp."""
    try:
        raw = client.get_product_raw(product_id)
    except Exception as exc:
        print(f"[manual-lookup/catalog-meta] {product_id}: {exc}", file=sys.stderr)
        return None, None

    if not isinstance(raw, dict):
        return None, None

    name = (
        raw.get("name")
        or raw.get("family_name")
        or raw.get("title")
        or None
    )
    name = str(name).strip() if name else None
    if name and re.fullmatch(r"MLBU?\d+", name, re.I):
        name = None

    picture = None
    pictures = raw.get("pictures") or []
    if isinstance(pictures, list):
        for row in pictures:
            if not isinstance(row, dict):
                continue
            candidate = (
                row.get("secure_url")
                or row.get("url")
                or row.get("src")
            )
            if candidate:
                picture = str(candidate).strip()
                break

    if not picture:
        candidate = raw.get("thumbnail") or raw.get("secure_thumbnail")
        if candidate:
            picture = str(candidate).strip()

    return name, picture


def _enrich_catalog_results(
    client: MercadoLivreClient,
    product_id: str,
    results: list[dict],
) -> tuple[list[dict], str | None]:
    name, picture = _catalog_display_metadata(client, product_id)
    enriched: list[dict] = []

    for source in results:
        row = dict(source or {})
        current_name = str(row.get("name") or "").strip()
        if not current_name or re.fullmatch(r"MLBU?\d+", current_name, re.I):
            if name:
                row["name"] = name
        if not str(row.get("picture") or "").strip() and picture:
            row["picture"] = picture
        enriched.append(row)

    return enriched, name


def lookup_reference(client: MercadoLivreClient, reference_url: str, limit: int = 3) -> dict:
    reference_url = str(reference_url or "").strip()
    if not reference_url:
        return {"mode":"reference","query":"","source":"none","results":[]}
    mlbu, item_id = _extract_reference_ids(reference_url)
    resolve_errors=[]
    exact_row=None

    # Caminho direto: quando a própria URL já revela o PRODUCT_ID (MLBU...),
    # seja em /up/MLBU..., /p/MLBU..., em ?pdp_filters=item_id:MLB... ou no
    # fragmento #wid=MLB..., consultamos a API oficial de catálogo direto por
    # esse ID. Isso cobre TANTO links com slug (.../nome-do-produto/up/MLBU...)
    # QUANTO links "enxutos" compartilhados pelo app (.../up/MLBU...?pdp_filters=...),
    # sem depender de raspar HTML (que o Mercado Livre pode bloquear com
    # verificação de conta) nem de uma chave paga do Serper.
    if mlbu:
        try:
            direct = lookup_product(client, mlbu, limit)
        except Exception as exc:
            direct = None
            resolve_errors.append(f"product_direct:{mlbu}: {exc}")
        if direct and direct.get("results"):
            exact_results, catalog_name = _enrich_catalog_results(
                client,
                mlbu,
                direct.get("results") or [],
            )

            similar_results = []
            if catalog_name:
                try:
                    similar_payload = lookup_term(client, catalog_name, max(limit * 3, 6))
                    similar_results = list(similar_payload.get("results") or [])
                except Exception as exc:
                    resolve_errors.append(f"similar_lookup:{catalog_name}: {exc}")

            merged = []
            seen = set()

            def add_candidate(row):
                if not isinstance(row, dict):
                    return
                key = (
                    str(row.get("item_id") or "").strip().upper()
                    or str(row.get("product_id") or "").strip().upper()
                    or str(row.get("url") or "").strip()
                    or f"{row.get('name')}:{row.get('price')}"
                )
                if not key or key in seen:
                    return
                seen.add(key)
                merged.append(row)

            # Sempre mantém primeiro o produto exato do link compartilhado.
            for row in exact_results:
                add_candidate(row)

            # Depois acrescenta alternativas relevantes.
            for row in similar_results:
                add_candidate(row)

            # Entre as alternativas já consideradas relevantes, prioriza menor preço.
            exact_count = len(exact_results)
            if len(merged) > exact_count:
                head = merged[:exact_count]
                tail = merged[exact_count:]
                tail.sort(
                    key=lambda r: (
                        r.get("price") is None,
                        float(r.get("price") or 10**18),
                        -float(r.get("discount_percent") or 0),
                    )
                )
                merged = head + tail

            return {
                "mode": "reference",
                "source": "product_direct+similar",
                "reference_url": reference_url,
                "reference_product_id": mlbu,
                "reference_item_id": item_id,
                "resolved_term": catalog_name,
                "resolve_errors": resolve_errors,
                "results": merged[:limit],
            }

    term = _term_from_reference_slug(reference_url)
    if str(term or "").strip().lower() in {"up", "p", "produto", "product"}:
        term = ""

    if not term:
        term, err = _resolve_reference_via_http(reference_url)
        if err:
            resolve_errors.append(f"http: {err}")
    if not term:
        term, exact_row, err = _resolve_reference_via_serper(reference_url, mlbu, item_id)
        if err:
            resolve_errors.append(f"serper: {err}")

    if term:
        payload = lookup_term(client, term, limit)
        payload.update({
            "mode": "reference",
            "reference_url": reference_url,
            "reference_product_id": mlbu,
            "reference_item_id": item_id,
            "resolved_term": term,
            "resolve_errors": resolve_errors,
        })
        if payload.get("results"):
            return payload

    # Último recurso: se conseguimos ao menos resolver a própria referência via
    # busca estruturada, devolve o produto exato com link afiliado em vez de zero.
    if exact_row:
        result = _public_result(exact_row, search_rank=1)
        return {
            "mode": "reference", "query": term or reference_url,
            "source": "reference_exact", "reference_url": reference_url,
            "reference_product_id": mlbu, "reference_item_id": item_id,
            "resolved_term": term, "resolve_errors": resolve_errors,
            "results": [result] if result.get("url") else [],
        }

    print(f"[manual-lookup/referencia] sem resolução: MLBU={mlbu or '-'} item={item_id or '-'} erros={' | '.join(resolve_errors) or '-'}", file=sys.stderr)
    return {
        "mode": "reference", "query": reference_url, "source": "none",
        "reference_url": reference_url, "reference_product_id": mlbu,
        "reference_item_id": item_id, "resolved_term": term,
        "resolve_errors": resolve_errors, "results": [],
    }

def main() -> None:
    parser = argparse.ArgumentParser(description="Consulta manual de produtos do Auvello")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--product-id")
    group.add_argument("--term")
    group.add_argument("--reference-url")
    parser.add_argument("--limit", type=int, default=3)
    args = parser.parse_args()

    limit = max(1, min(args.limit, 3))
    client = MercadoLivreClient()

    # Clientes internos podem registrar diagnósticos em stdout. Para o Node receber
    # JSON limpo, esses logs vão para stderr e somente o resultado sai em stdout.
    with contextlib.redirect_stdout(sys.stderr):
        if args.product_id:
            payload = lookup_product(client, args.product_id.strip().upper(), limit)
        elif args.reference_url:
            payload = lookup_reference(client, args.reference_url.strip(), limit)
        else:
            payload = lookup_term(client, args.term.strip(), limit)

    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
