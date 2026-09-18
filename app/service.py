from __future__ import annotations

from dataclasses import dataclass
import math
import re
import time
import unicodedata

from .affiliate import AffiliateClient, AffiliateError
from .classifier import Classifier
from .config import settings
from .database import Database
from .discovery import Discovery
from .formatter import build_message
from .mercado_livre import MercadoLivreClient
from .models import Product
from .shopee import ShopeeClient
from .whatsapp import WhatsAppClient


GENERAL_GROUP = "geral"


@dataclass
class Candidate:
    product: Product
    group: str
    previous_price: float | None
    drop_percent: float
    score: float = 0.0

    @property
    def effective_discount(self) -> float:
        return max(self.product.discount_percent, self.drop_percent)

    @property
    def savings(self) -> float:
        price = self.product.price or 0.0
        immediate = 0.0
        if self.product.original_price and self.product.original_price > price:
            immediate = self.product.original_price - price
        historical = 0.0
        if self.previous_price and self.previous_price > price:
            historical = self.previous_price - price
        return max(immediate, historical)


class AuvelloService:
    def __init__(self) -> None:
        self.ml = MercadoLivreClient()
        self.shopee = ShopeeClient()
        self.db = Database()
        self.discovery = Discovery(self.ml, self.db, self.shopee)
        self.classifier = Classifier(self.ml)
        self.affiliate = AffiliateClient()
        self.whatsapp = WhatsAppClient()
        self._last_send_at: float | None = None
        self._messages_sent_this_cycle = 0

    # Compatibilidade com --once: faz uma descoberta e publica os dois fluxos.
    def run_once(self) -> None:
        self.run_discovery()
        self.run_specific_groups()
        self.run_general_group()

    def run_discovery(self) -> None:
        print("\n=== AUVELLO: descoberta central ===")
        offers = self.discovery.run()
        print(f"[discovery] {len(offers)} ofertas unicas recebidas pelo servico")

        candidates_by_product: dict[str, list[Candidate]] = {}
        without_group = 0
        not_qualified = 0

        for offer in offers:
            try:
                candidate = self._evaluate(offer)
            except Exception as exc:
                print(f"[oferta] {offer.item_id or offer.product_id}: {exc}")
                continue

            if candidate is None:
                group = offer.forced_group or self.classifier.classify(offer)
                if not group:
                    without_group += 1
                else:
                    not_qualified += 1
                continue

            product_key = offer.product_id or offer.item_id
            if product_key:
                candidates_by_product.setdefault(product_key, []).append(candidate)

        best_per_product: list[Candidate] = []
        suppressed_siblings = 0
        for candidates in candidates_by_product.values():
            best = min(candidates, key=self._candidate_sort_key)
            best_per_product.append(best)
            suppressed_siblings += max(0, len(candidates) - 1)

        by_group: dict[str, list[Candidate]] = {}
        for c in best_per_product:
            by_group.setdefault(c.group, []).append(c)
        for pool in by_group.values():
            self._assign_scores(pool)

        records = [self._candidate_to_record(c) for c in best_per_product]
        updated = self.db.upsert_offer_candidates(records)
        pruned = self.db.prune_offer_candidates()

        print(
            f"[cache] {updated} candidatos atualizados; {pruned} expirados removidos; "
            f"TTL={settings.candidate_cache_ttl_minutes} min"
        )
        print(
            f"[controle] {sum(len(v) for v in candidates_by_product.values())} "
            f"ofertas elegiveis -> {len(best_per_product)} PRODUCTs; "
            f"{suppressed_siblings} ofertas irmas suprimidas"
        )
        if without_group:
            print(f"[controle] {without_group} ofertas sem grupo")
        if not_qualified:
            print(f"[controle] {not_qualified} ofertas sem alerta")
        print("=== AUVELLO: descoberta finalizada ===\n")

    def run_specific_groups(self) -> None:
        print("\n=== AUVELLO: grupos especificos ===")
        self._begin_send_cycle()
        rows = self.db.offer_candidates()
        candidates = [self._candidate_from_record(row) for row in rows]

        by_group: dict[str, list[Candidate]] = {}
        for candidate in candidates:
            if candidate.group == GENERAL_GROUP:
                continue
            by_group.setdefault(candidate.group, []).append(candidate)

        selected: list[Candidate] = []
        for group, pool in by_group.items():
            self._assign_scores(pool)
            available = [c for c in pool if self.db.can_notify(c.product, group)]
            group_selected = self._select_opportunity_slots(available)
            group_selected = self._ensure_marketplace_slot(
                selected=group_selected,
                pool=available,
                marketplace="shopee",
                limit=settings.max_products_per_group,
            )
            selected.extend(group_selected)
            print(
                f"[slots] {group}: {len(pool)} no cache, {len(available)} fora do cooldown -> "
                f"{len(group_selected)} selecionados"
            )

        # Alterna oportunidades por score, mantendo o teto de cada grupo já
        # aplicado acima. Cada envio confirmado é espelhado imediatamente.
        selected.sort(key=lambda c: -c.score)
        for candidate in selected:
            if self._message_limit_reached():
                print(f"[fila] airbag atingido: {settings.max_messages_per_cycle} mensagens")
                break
            try:
                self._publish_specific_and_mirror(candidate)
            except Exception as exc:
                print(f"[produto] {candidate.product.product_id}: {exc}")

        print(f"[fila] {self._messages_sent_this_cycle} mensagens enviadas neste ciclo especifico")
        print("=== AUVELLO: grupos especificos finalizados ===\n")

    def run_general_group(self) -> None:
        print("\n=== AUVELLO: grupo Geral ===")
        self._begin_send_cycle()
        rows = self.db.offer_candidates()
        candidates = [self._candidate_from_record(row) for row in rows]
        available = [c for c in candidates if self.db.can_notify(c.product, GENERAL_GROUP)]
        self._assign_scores(available)
        strong = [c for c in available if self._is_strong_for_general(c, settings.general_routine_min_score)]
        recent_groups = self.db.recent_general_source_groups()
        recent_types = self.db.recent_general_variety_keys()
        # Havendo uma oferta elegível da Shopee, ela recebe prioridade para
        # garantir presença no lote do Geral sem furar cooldown/diversidade.
        strong.sort(key=lambda c: (c.product.marketplace == "shopee", self.db.is_price_drop_exception(c.product, GENERAL_GROUP), c.group not in recent_groups, self._product_type_key(c.product) not in recent_types, c.score, c.effective_discount, c.savings), reverse=True)
        remaining_hour = self._general_remaining_hourly_capacity()
        target = min(settings.max_products_general, remaining_hour)
        selected = []
        used_groups = set()
        used_types = set()
        for candidate in strong:
            if len(selected) >= target:
                break
            type_key = self._product_type_key(candidate.product)
            price_drop_exception = self.db.is_price_drop_exception(candidate.product, GENERAL_GROUP)
            if not price_drop_exception:
                if candidate.group in recent_groups or candidate.group in used_groups:
                    continue
                if type_key and (type_key in recent_types or type_key in used_types):
                    continue
            selected.append(candidate)
            used_groups.add(candidate.group)
            if type_key:
                used_types.add(type_key)
        print(f"[geral] {len(candidates)} candidatos no cache, {len(available)} fora do cooldown, {len(strong)} fortes -> {len(selected)} selecionados | categorias recentes={len(recent_groups)} | tipos recentes={len(recent_types)} | cota hora restante={remaining_hour}")
        if remaining_hour <= 0:
            print(f"[geral] teto de {settings.general_max_messages_per_hour} mensagens/hora atingido; rotina sem envio")
        for candidate in selected:
            if self._message_limit_reached() or self._general_hourly_limit_reached():
                break
            try:
                self._publish_general(candidate, reason="rotina")
            except Exception as exc:
                print(f"[geral] {candidate.product.product_id}: {exc}")
        print(f"[fila] {self._messages_sent_this_cycle} mensagens enviadas na rotina Geral")
        print("=== AUVELLO: grupo Geral finalizado ===\n")

    def _evaluate(self, product: Product) -> Candidate | None:
        group = product.forced_group or self.classifier.classify(product)
        if not group:
            print(f"[ignorado] sem grupo: {product.name}")
            return None

        self.db.record_price(product)
        previous = self.db.previous_price(product)
        drop_percent = 0.0
        if previous and product.price and previous > product.price:
            drop_percent = (1 - product.price / previous) * 100

        qualifies = (
            product.discount_percent >= settings.min_discount_percent
            or drop_percent >= settings.min_price_drop_percent
        )
        if not qualifies:
            return None

        return Candidate(product, group, previous, drop_percent)

    @staticmethod
    def _candidate_sort_key(candidate: Candidate) -> tuple[float, float, float]:
        price = candidate.product.price if candidate.product.price is not None else float("inf")
        return (price, -candidate.product.discount_percent, -candidate.drop_percent)

    @staticmethod
    def _norm(value: float, values: list[float]) -> float:
        if not values:
            return 0.0
        lo, hi = min(values), max(values)
        if hi <= lo:
            return 1.0 if value > 0 else 0.0
        return (value - lo) / (hi - lo)

    def _assign_scores(self, pool: list[Candidate]) -> None:
        if not pool:
            return
        discounts = [c.effective_discount for c in pool]
        savings_log = [math.log1p(max(0.0, c.savings)) for c in pool]
        prices_log = [math.log1p(max(0.0, c.product.price or 0.0)) for c in pool]

        for c in pool:
            discount_n = self._norm(c.effective_discount, discounts)
            saving_n = self._norm(math.log1p(max(0.0, c.savings)), savings_log)
            price_n = self._norm(math.log1p(max(0.0, c.product.price or 0.0)), prices_log)
            accessibility_n = 1.0 - price_n
            c.score = 100.0 * (
                settings.score_discount_weight * discount_n
                + settings.score_savings_weight * saving_n
                + settings.score_accessibility_weight * accessibility_n
            )

    def _select_opportunity_slots(self, pool: list[Candidate]) -> list[Candidate]:
        remaining = list(pool)
        chosen: list[Candidate] = []

        def take_best(eligible, key) -> Candidate | None:
            options = [c for c in remaining if eligible(c)]
            if not options:
                return None
            best = max(options, key=key)
            remaining.remove(best)
            chosen.append(best)
            return best

        for _ in range(settings.slot_top_discount_count):
            take_best(
                lambda c: c.effective_discount >= settings.slot_min_strong_discount,
                lambda c: (c.effective_discount, c.score, c.savings),
            )

        take_best(
            lambda c: (
                c.effective_discount >= settings.min_discount_percent
                and c.savings >= settings.slot_min_savings_reais
            ),
            lambda c: (c.savings, c.effective_discount, c.score),
        )

        take_best(
            lambda c: (
                (c.product.price or float("inf")) <= settings.slot_accessible_max_price
                and c.effective_discount >= settings.slot_accessible_min_discount
            ),
            lambda c: (c.effective_discount, c.score, c.savings),
        )

        take_best(
            lambda c: (
                c.score >= settings.slot_min_score
                and c.effective_discount >= settings.min_discount_percent
            ),
            lambda c: (c.score, c.effective_discount, c.savings),
        )

        return chosen[: settings.max_products_per_group]

    @staticmethod
    def _ensure_marketplace_slot(
        selected: list[Candidate],
        pool: list[Candidate],
        marketplace: str,
        limit: int,
    ) -> list[Candidate]:
        """Reserva uma vaga para uma loja, mas só entre ofertas elegíveis.

        O pool já passou por desconto, cooldown e classificação. Portanto a
        reserva não força produto ruim; apenas evita que o score deixe a loja
        inteira fora do lote.
        """
        result = list(selected)
        if any(c.product.marketplace == marketplace for c in result):
            return result[:limit]

        candidates = [
            c for c in pool
            if c.product.marketplace == marketplace and c not in result
        ]
        if not candidates or limit <= 0:
            return result[:limit]

        best = max(
            candidates,
            key=lambda c: (c.score, c.effective_discount, c.savings),
        )
        if len(result) < limit:
            result.append(best)
            return result

        replaceable = [
            (index, candidate)
            for index, candidate in enumerate(result)
            if candidate.product.marketplace != marketplace
        ]
        if replaceable:
            index, _ = min(
                replaceable,
                key=lambda pair: (
                    pair[1].score,
                    pair[1].effective_discount,
                    pair[1].savings,
                ),
            )
            result[index] = best
        return result[:limit]

    @staticmethod
    def _product_type_key(product: Product) -> str:
        text = unicodedata.normalize("NFKD", str(product.name or ""))
        text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
        tokens = re.findall(r"[a-z0-9]+", text)
        noise = {"kit", "jogo", "conjunto", "produto", "oferta", "novo", "nova", "original", "para", "com", "sem", "de", "da", "do", "das", "dos", "um", "uma", "unidade", "unidades", "peca", "pecas"}
        for token in tokens:
            if token in noise or token.isdigit() or len(token) < 3:
                continue
            return token
        return (product.domain_id or product.category_id or "").lower()

    def _general_diversity_allows(self, candidate: Candidate) -> bool:
        if self.db.is_price_drop_exception(candidate.product, GENERAL_GROUP):
            return True
        if candidate.group in self.db.recent_general_source_groups():
            return False
        type_key = self._product_type_key(candidate.product)
        return not type_key or type_key not in self.db.recent_general_variety_keys()

    @staticmethod
    def _is_strong_for_general(candidate: Candidate, min_score: float) -> bool:
        """Critério premium do Geral: basta cumprir um dos três sinais."""
        return (
            candidate.effective_discount >= settings.general_min_discount_percent
            or candidate.savings >= settings.general_min_savings_brl
            or candidate.score >= min_score
        )

    def _general_messages_last_hour(self) -> int:
        return self.db.count_group_notifications_since(GENERAL_GROUP, minutes=60)

    def _general_remaining_hourly_capacity(self) -> int:
        limit = settings.general_max_messages_per_hour
        if limit <= 0:
            return 10**9
        return max(0, limit - self._general_messages_last_hour())

    def _general_hourly_limit_reached(self) -> bool:
        limit = settings.general_max_messages_per_hour
        return limit > 0 and self._general_messages_last_hour() >= limit

    def _publish_specific_and_mirror(self, candidate: Candidate) -> None:
        product, group = candidate.product, candidate.group
        if not self.db.can_notify(product, group):
            print(f"[cooldown] {group}: {product.name}")
            return

        affiliate_url = self._affiliate_url(product)
        if not affiliate_url:
            return
        message = build_message(product, affiliate_url, candidate.previous_price)

        if self._queued_send(group, message, product.picture):
            self.db.mark_notified(product, group, source_group_key=group, variety_key=self._product_type_key(product))
            print(
                f"[enviado] {group}: {product.name} | R$ {(product.price or 0):.2f} | "
                f"score={candidate.score:.1f}"
            )

            # Regra Auvello: específico -> Geral imediatamente, independente
            # do relógio de 5 minutos. O inverso nunca acontece.
            mirror_enabled = self.db.category_mirrors_to_general(group)
            strong_for_general = self._is_strong_for_general(candidate, settings.general_min_score)
            if not mirror_enabled:
                print(f"[espelho] desativado para {group}: {product.name}")
            elif not strong_for_general:
                print(
                    f"[espelho] oferta nao forte o suficiente para o Geral: {product.name} | "
                    f"desc={candidate.effective_discount:.1f}% | economia=R$ {candidate.savings:.2f} | "
                    f"score={candidate.score:.1f}"
                )
            elif self._general_hourly_limit_reached():
                print(
                    f"[espelho] teto do Geral atingido ({settings.general_max_messages_per_hour}/hora): "
                    f"{product.name}"
                )
            elif not self._message_limit_reached() and self.db.can_notify(product, GENERAL_GROUP):
                if not self._general_diversity_allows(candidate):
                    print(f"[espelho] variedade do Geral segurou: {product.name}")
                elif self._queued_send(GENERAL_GROUP, message, product.picture):
                    self.db.mark_notified(product, GENERAL_GROUP, source_group_key=group, variety_key=self._product_type_key(product))
                    print(f"[espelho] {group} -> geral: {product.name}")
            else:
                print(f"[espelho] geral em cooldown: {product.name}")

    def _publish_general(self, candidate: Candidate, reason: str) -> None:
        product = candidate.product
        if not self.db.can_notify(product, GENERAL_GROUP):
            print(f"[cooldown] geral: {product.name}")
            return
        affiliate_url = self._affiliate_url(product)
        if not affiliate_url:
            return
        message = build_message(product, affiliate_url, candidate.previous_price)
        if not self._general_diversity_allows(candidate):
            print(f"[variedade] geral/{reason}: segurado {product.name}")
            return
        if self._queued_send(GENERAL_GROUP, message, product.picture):
            self.db.mark_notified(product, GENERAL_GROUP, source_group_key=candidate.group, variety_key=self._product_type_key(product))
            print(
                f"[enviado] geral/{reason}: {product.name} | "
                f"R$ {(product.price or 0):.2f} | score={candidate.score:.1f}"
            )

    def _affiliate_url(self, product: Product) -> str | None:
        # O offerLink retornado pela API da Shopee já contém o rastreamento do
        # afiliado. Passá-lo pelo gerador do Mercado Livre destruiria o link.
        if product.marketplace == "shopee":
            return product.permalink or None
        try:
            return self.affiliate.build(product.permalink)
        except AffiliateError as exc:
            print(f"[afiliado] {product.name}: {exc}")
            return None

    def _queued_send(self, group: str, message: str, image_url: str | None = None) -> bool:
        if self._last_send_at is not None:
            elapsed = time.monotonic() - self._last_send_at
            wait = max(0.0, settings.whatsapp_send_delay_seconds - elapsed)
            if wait > 0:
                print(f"[fila] aguardando {wait:.1f}s para proximo envio")
                time.sleep(wait)
        group_id = None if group == GENERAL_GROUP else self.db.category_group_id(group)
        ok = self.whatsapp.send(
            group,
            message,
            image_url=image_url,
            group_id_override=group_id,
        )
        self._last_send_at = time.monotonic()
        if ok:
            self._messages_sent_this_cycle += 1
        return ok

    def _begin_send_cycle(self) -> None:
        self._messages_sent_this_cycle = 0

    def _message_limit_reached(self) -> bool:
        limit = settings.max_messages_per_cycle
        return limit > 0 and self._messages_sent_this_cycle >= limit

    @staticmethod
    def _candidate_to_record(candidate: Candidate) -> dict:
        p = candidate.product
        return {
            "catalog_product_key": p.product_id or p.item_id,
            "product_id": p.product_id,
            "item_id": p.item_id,
            "name": p.name,
            "category_id": p.category_id,
            "domain_id": p.domain_id,
            "price": p.price or 0.0,
            "original_price": p.original_price,
            "currency_id": p.currency_id,
            "permalink": p.permalink,
            "picture": p.picture,
            "forced_group": p.forced_group,
            "discovery_source": p.discovery_source,
            "marketplace": p.marketplace,
            "group_key": candidate.group,
            "previous_price": candidate.previous_price,
            "drop_percent": candidate.drop_percent,
            "score": candidate.score,
        }

    @staticmethod
    def _candidate_from_record(row: dict) -> Candidate:
        product = Product(
            product_id=row.get("product_id") or row.get("catalog_product_key") or "",
            item_id=row.get("item_id"),
            name=row.get("name") or "Produto",
            category_id=row.get("category_id"),
            domain_id=row.get("domain_id"),
            price=float(row.get("price") or 0),
            original_price=(float(row["original_price"]) if row.get("original_price") is not None else None),
            currency_id=row.get("currency_id") or "BRL",
            permalink=row.get("permalink") or "",
            picture=row.get("picture"),
            forced_group=row.get("forced_group"),
            discovery_source=row.get("discovery_source"),
            marketplace=row.get("marketplace") or "mercado_livre",
        )
        return Candidate(
            product=product,
            group=row.get("group_key") or "",
            previous_price=(float(row["previous_price"]) if row.get("previous_price") is not None else None),
            drop_percent=float(row.get("drop_percent") or 0),
            score=float(row.get("score") or 0),
        )
