from __future__ import annotations

import json
from pathlib import Path
from collections import Counter

from .mercado_livre import MercadoLivreClient
from .shopee import ShopeeClient
from .models import Product
from .config import settings
from .database import Database




def _split_search_terms(raw: str, max_terms: int = 15) -> list[str]:
    """Aceita uma busca simples ou vários termos separados por vírgula,
    ponto e vírgula ou quebra de linha. Remove duplicados preservando ordem.
    """
    if not raw:
        return []
    normalized = str(raw).replace('\r\n', '\n').replace('\r', '\n').replace(';', '\n').replace(',', '\n')
    seen: set[str] = set()
    terms: list[str] = []
    for part in normalized.split('\n'):
        term = ' '.join(part.split()).strip()
        key = term.casefold()
        if not term or key in seen:
            continue
        seen.add(key)
        terms.append(term)
        if len(terms) >= max_terms:
            break
    return terms

AUVELLO_ROOTS = {
    "eletronicos_tecnologia": ["MLB1000", "MLB1648"],
    "moda_vestuario": ["MLB1430"],
    "celulares_acessorios": ["MLB1051"],
    "games_acessorios": ["MLB1144"],
    "utilidades_domesticas": ["MLB1574", "MLB5726"],
    "pet_shop": ["MLB1071"],
}

SHOPEE_DEFAULT_TERMS = {
    "eletronicos_tecnologia": ["notebook", "smart tv"],
    "moda_vestuario": ["tênis", "roupa masculina"],
    "celulares_acessorios": ["smartphone", "fone bluetooth"],
    "games_acessorios": ["console videogame", "controle gamer"],
    "utilidades_domesticas": ["air fryer", "aspirador robô"],
    "pet_shop": ["ração cachorro", "ração gato"],
}


class Discovery:
    """
    OFERTA é a entidade principal.

    Highlights / Watchlist / Trends -> PRODUCT
    PRODUCT -> /products/{id}/items -> ofertas reais

    Não depende de buy_box_winner.
    Não depende de pai/filho.
    """

    def __init__(
        self,
        ml: MercadoLivreClient,
        db: Database | None = None,
        shopee: ShopeeClient | None = None,
    ) -> None:
        self.ml = ml
        self.db = db
        self.shopee = shopee
        self._shopee_term_cursor = 0

    def run(self) -> list[Product]:
        offers: dict[str, Product] = {}

        if getattr(settings, "discovery_highlights", True):
            self._from_highlights(offers)

        if getattr(settings, "discovery_watchlist", True):
            self._from_watchlist(offers)

        if getattr(settings, "discovery_trends", True):
            self._from_trends(offers)

        # Categorias criadas no Admin podem ter um termo próprio de busca.
        # Isso permite criar um novo grupo/categoria sem alterar o código.
        self._from_dynamic_categories(offers)

        # Pedidos aprovados da comunidade são interesses/termos de busca.
        # O link enviado pelo membro é só referência; não fixa aquele anúncio.
        self._from_community(offers)

        # A Shopee entra no mesmo cache, regras, score e fila do Mercado Livre.
        # Uma falha nela não impede o restante da descoberta.
        self._from_shopee(offers)

        # Produtos fixados pelo dev entram por ultimo para que o grupo escolhido
        # no Admin prevaleca se a mesma oferta tambem vier de outra fonte.
        # Continua sendo uma fonte aditiva: nao substitui Highlights/Watchlist/Trends.
        self._from_admin(offers)

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

        out[f"{offer.marketplace}:{offer.item_id}"] = offer
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

    def _from_dynamic_categories(self, out: dict[str, Product]) -> None:
        """Descobre ofertas para categorias criadas/gerenciadas no Admin.

        Somente categorias com ``search_term`` preenchido geram chamadas extras.
        Os resultados recebem ``forced_group`` para irem ao grupo cadastrado.
        """
        if self.db is None:
            return
        try:
            categories = self.db.active_search_categories()
        except Exception as exc:
            print(f"[categorias] erro lendo categorias: {exc}")
            return
        if not categories:
            return

        min_discount = max(1, int(getattr(settings, "min_discount_percent", 15)))
        for entry in categories:
            group_key = (entry.get("group_key") or "").strip()
            raw_search = (entry.get("search_term") or "").strip()
            name = (entry.get("name") or group_key).strip()
            terms = _split_search_terms(raw_search)
            if not group_key or not terms:
                continue

            total_products = 0
            total_found = 0
            successful_terms = 0
            for search_term in terms:
                try:
                    results = self.ml.search_products(q=search_term)
                    found = 0
                    for result in results:
                        offers = self.ml.offers_from_search_result(
                            result, discounted_only=True, min_discount=min_discount
                        )
                        for offer in offers:
                            offer.forced_group = group_key
                            offer.discovery_source = "admin_category"
                        found += self._add_many(out, offers)
                    successful_terms += 1
                    total_products += len(results)
                    total_found += found
                    print(
                        f"[categorias] {name!r} termo={search_term!r} -> {group_key}: "
                        f"{len(results)} PRODUCTs, {found} ofertas >= {min_discount}%"
                    )
                except Exception as exc:
                    print(f"[categorias] {name!r} termo={search_term!r}: {exc}")

            print(
                f"[categorias] {name!r}: {successful_terms}/{len(terms)} termos processados, "
                f"{total_products} PRODUCTs consultados, {total_found} ofertas adicionadas"
            )

    def _from_community(self, out: dict[str, Product]) -> None:
        """
        Usa apenas pedidos que o dev aprovou no Admin.

        Cada pedido aprovado vira um TERMO DE BUSCA recorrente e associado ao
        grupo validado pelo dev. O PRODUCT_ID do link de referência não é
        monitorado diretamente por esta fonte.
        """
        if self.db is None:
            return

        try:
            requests = self.db.approved_community_requests()
        except Exception as exc:
            print(f"[community] erro lendo pedidos aprovados: {exc}")
            return

        if not requests:
            print("[community] nenhum pedido aprovado")
            return

        min_discount = max(1, int(getattr(settings, "min_discount_percent", 15)))

        for entry in requests:
            request_id = entry.get("id")
            search_term = (entry.get("search_term") or "").strip()
            group_key = entry.get("group_key")
            if not search_term or not group_key:
                continue

            try:
                results = self.ml.search_products(q=search_term)
                found = 0
                for result in results:
                    offers = self.ml.offers_from_search_result(
                        result,
                        discounted_only=True,
                        min_discount=min_discount,
                    )
                    for offer in offers:
                        offer.forced_group = group_key
                        offer.discovery_source = "community"
                    found += self._add_many(out, offers)

                print(
                    f"[community] #{request_id} {search_term!r} -> {group_key}: "
                    f"{len(results)} PRODUCTs, {found} ofertas >= {min_discount}%"
                )
            except Exception as exc:
                print(f"[community] #{request_id} {search_term!r}: {exc}")

    def _from_admin(self, out: dict[str, Product]) -> None:
        """
        Busca os PRODUCT_IDs cadastrados no Auvello Admin em toda rodada.
        O grupo escolhido no painel e aplicado como override apenas a esses
        produtos; todas as regras de elegibilidade/score/slots continuam.
        """
        if self.db is None:
            return

        try:
            monitored = self.db.active_admin_products()
        except Exception as exc:
            print(f"[admin] erro lendo produtos monitorados: {exc}")
            return

        if not monitored:
            print("[admin] nenhum produto fixado")
            return

        for entry in monitored:
            product_id = entry.get("product_id")
            group_key = entry.get("group_key")
            if not product_id or not group_key:
                continue
            try:
                offers = self.ml.product_offers(product_id)
                for offer in offers:
                    offer.forced_group = group_key
                    offer.discovery_source = "admin"
                found = self._add_many(out, offers)
                print(
                    f"[admin] {product_id} -> {group_key}: "
                    f"{len(offers)} ofertas, {found} novas"
                )
            except Exception as exc:
                print(f"[admin] {product_id}: {exc}")

    def _shopee_search_terms(self) -> list[tuple[str, str | None, str]]:
        """Monta termos da configuração existente sem exigir painel novo."""
        entries: list[tuple[str, str | None, str]] = []

        if self.db is not None:
            try:
                for row in self.db.active_search_categories():
                    group = (row.get("group_key") or "").strip() or None
                    for term in _split_search_terms(row.get("search_term") or ""):
                        entries.append((term, group, "admin_category"))
            except Exception as exc:
                print(f"[shopee] categorias dinâmicas: {exc}")

            try:
                for row in self.db.approved_community_requests():
                    term = (row.get("search_term") or "").strip()
                    group = (row.get("group_key") or "").strip() or None
                    if term:
                        entries.append((term, group, "community"))
            except Exception as exc:
                print(f"[shopee] pedidos da comunidade: {exc}")

        if settings.discovery_watchlist:
            path = Path("watchlist.json")
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    for term in data.get("queries", []):
                        clean = " ".join(str(term).split()).strip()
                        if clean:
                            entries.append((clean, None, "watchlist"))
                except Exception as exc:
                    print(f"[shopee] watchlist: {exc}")

        for group, terms in SHOPEE_DEFAULT_TERMS.items():
            for term in terms:
                entries.append((term, group, "default"))

        unique: list[tuple[str, str | None, str]] = []
        positions: dict[str, int] = {}
        for term, group, source in entries:
            key = term.casefold()
            if key in positions:
                # Se o termo repetido mais recente conhece o grupo e o
                # primeiro não, aproveita a classificação sem repetir a API.
                index = positions[key]
                old_term, old_group, old_source = unique[index]
                if not old_group and group:
                    unique[index] = (old_term, group, source)
                continue
            positions[key] = len(unique)
            unique.append((term, group, source))
        return unique

    def _from_shopee(self, out: dict[str, Product]) -> None:
        if self.shopee is None or not self.shopee.configured:
            print("[shopee] desativada ou sem SHOPEE_APP_ID/SHOPEE_SECRET")
            return

        terms = self._shopee_search_terms()
        if not terms:
            print("[shopee] nenhum termo de busca configurado")
            return

        limit = max(1, min(settings.shopee_max_terms_per_cycle, len(terms)))
        start = self._shopee_term_cursor % len(terms)
        selected = [terms[(start + offset) % len(terms)] for offset in range(limit)]
        self._shopee_term_cursor = (start + limit) % len(terms)

        total = 0
        for term, forced_group, source in selected:
            try:
                offers = self.shopee.search_offers(term)
                for offer in offers:
                    if forced_group:
                        offer.forced_group = forced_group
                    offer.discovery_source = f"shopee_{source}"
                added = self._add_many(out, offers)
                total += added
                print(
                    f"[shopee] {term!r} -> {forced_group or 'classificação automática'}: "
                    f"{len(offers)} recebidas, {added} adicionadas"
                )
            except Exception as exc:
                print(f"[shopee] {term!r}: {exc}")

        print(f"[shopee] {len(selected)}/{len(terms)} termos processados, {total} ofertas adicionadas")

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
