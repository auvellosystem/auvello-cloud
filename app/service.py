from __future__ import annotations

from dataclasses import dataclass
import math
import time

from .affiliate import AffiliateClient, AffiliateError
from .classifier import Classifier
from .config import settings
from .database import Database
from .discovery import Discovery
from .formatter import build_message
from .mercado_livre import MercadoLivreClient
from .models import Product
from .whatsapp import WhatsAppClient


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
        self.discovery = Discovery(self.ml)
        self.classifier = Classifier(self.ml)
        self.affiliate = AffiliateClient()
        self.db = Database()
        self.whatsapp = WhatsAppClient()
        self._last_send_at: float | None = None
        self._messages_sent_this_run = 0

    def run_once(self) -> None:
        print("\n=== AUVELLO: iniciando rodada ===")
        offers = self.discovery.run()
        print(f"[discovery] {len(offers)} ofertas unicas recebidas pelo servico")

        candidates_by_product: dict[str, list[Candidate]] = {}
        without_group = 0
        not_qualified = 0

        # Analisa todas as ofertas e preserva histórico por ITEM/oferta.
        for offer in offers:
            try:
                candidate = self._evaluate(offer)
            except Exception as exc:
                print(f"[oferta] {offer.item_id or offer.product_id}: {exc}")
                continue

            if candidate is None:
                group = self.classifier.classify(offer)
                if not group:
                    without_group += 1
                else:
                    not_qualified += 1
                continue

            product_key = offer.product_id or offer.item_id
            if product_key:
                candidates_by_product.setdefault(product_key, []).append(candidate)

        # UMA oferta por PRODUCT_ID: primeiro elegibilidade por oferta; depois
        # escolhe a oferta de menor preço daquele produto.
        best_per_product: list[Candidate] = []
        suppressed_siblings = 0
        for candidates in candidates_by_product.values():
            best = min(candidates, key=self._candidate_sort_key)
            best_per_product.append(best)
            suppressed_siblings += max(0, len(candidates) - 1)

        # Auvello Score é calculado dentro de cada grupo para comparar coisas
        # comparáveis: 45% desconto + 30% economia em R$ + 25% acessibilidade.
        by_group: dict[str, list[Candidate]] = {}
        for c in best_per_product:
            by_group.setdefault(c.group, []).append(c)
        for pool in by_group.values():
            self._assign_scores(pool)

        # Slots de oportunidade, por grupo. Slot vazio NÃO é preenchido por
        # produto fraco só para alcançar a quantidade máxima.
        selected: list[Candidate] = []
        for group, pool in by_group.items():
            group_selected = self._select_opportunity_slots(pool)
            selected.extend(group_selected)
            print(
                f"[slots] {group}: {len(pool)} produtos elegiveis -> "
                f"{len(group_selected)} selecionados"
            )
            for c in group_selected:
                print(
                    f"  [score] {c.product.name[:70]} | "
                    f"{c.effective_discount:.1f}% | economia R$ {c.savings:.2f} | "
                    f"preco R$ {(c.product.price or 0):.2f} | score {c.score:.1f}"
                )

        print(
            f"[controle] {sum(len(v) for v in candidates_by_product.values())} "
            f"ofertas elegiveis -> {len(best_per_product)} PRODUCTs; "
            f"{suppressed_siblings} ofertas irmas suprimidas; "
            f"{len(selected)} oportunidades selecionadas"
        )
        if without_group:
            print(f"[controle] {without_group} ofertas sem grupo")
        if not_qualified:
            print(f"[controle] {not_qualified} ofertas sem alerta")

        self._messages_sent_this_run = 0
        self._last_send_at = None

        # Alterna os grupos por score para não concentrar toda a fila em um
        # único grupo quando houver um limite global de segurança.
        selected.sort(key=lambda c: -c.score)
        for candidate in selected:
            if self._message_limit_reached():
                print(f"[fila] limite global atingido: {settings.max_messages_per_run} mensagens")
                break
            try:
                self._publish(candidate)
            except Exception as exc:
                print(f"[produto] {candidate.product.product_id}: {exc}")

        print(f"[fila] {self._messages_sent_this_run} mensagens enviadas nesta rodada")
        print("=== AUVELLO: rodada finalizada ===\n")

    def _evaluate(self, product: Product) -> Candidate | None:
        group = self.classifier.classify(product)
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
        discounts = [c.effective_discount for c in pool]
        # log1p evita que um produto caríssimo domine a economia em reais.
        savings_log = [math.log1p(max(0.0, c.savings)) for c in pool]
        prices_log = [math.log1p(max(0.0, c.product.price or 0.0)) for c in pool]

        for c in pool:
            discount_n = self._norm(c.effective_discount, discounts)
            saving_n = self._norm(math.log1p(max(0.0, c.savings)), savings_log)
            # Menor preço = maior acessibilidade.
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

        # 2 vagas: maiores descontos absolutos. Exige desconto forte.
        for _ in range(settings.slot_top_discount_count):
            take_best(
                lambda c: c.effective_discount >= settings.slot_min_strong_discount,
                lambda c: (c.effective_discount, c.score, c.savings),
            )

        # 1 vaga: maior economia em reais, mas ainda exige desconto mínimo.
        take_best(
            lambda c: (
                c.effective_discount >= settings.min_discount_percent
                and c.savings >= settings.slot_min_savings_reais
            ),
            lambda c: (c.savings, c.effective_discount, c.score),
        )

        # 1 vaga: produto acessível + desconto realmente bom.
        take_best(
            lambda c: (
                (c.product.price or float("inf")) <= settings.slot_accessible_max_price
                and c.effective_discount >= settings.slot_accessible_min_discount
            ),
            lambda c: (c.effective_discount, c.score, c.savings),
        )

        # 1 vaga: melhor oportunidade geral. Score mínimo impede preencher
        # a vaga com qualquer coisa só porque sobrou espaço.
        take_best(
            lambda c: (
                c.score >= settings.slot_min_score
                and c.effective_discount >= settings.min_discount_percent
            ),
            lambda c: (c.score, c.effective_discount, c.savings),
        )

        return chosen[: settings.max_products_per_group]

    def _publish(self, candidate: Candidate) -> None:
        product, group = candidate.product, candidate.group
        normal_allowed = self.db.can_notify(product, group)
        big_allowed = (
            product.discount_percent >= settings.big_discount_percent
            and self.db.can_notify(product, "maiores_descontos")
        )
        if not normal_allowed and not big_allowed:
            print(f"[cooldown] {product.name}")
            return

        try:
            affiliate_url = self.affiliate.build(product.permalink)
        except AffiliateError as exc:
            print(f"[afiliado] {product.name}: {exc}")
            return

        message = build_message(product, affiliate_url, candidate.previous_price)
        if normal_allowed and not self._message_limit_reached():
            if self._queued_send(group, message, product.picture):
                self.db.mark_notified(product, group)
                print(f"[enviado] {group}: {product.name} | R$ {product.price:.2f} | score={candidate.score:.1f}")

        if big_allowed and not self._message_limit_reached():
            if self._queued_send("maiores_descontos", message, product.picture):
                self.db.mark_notified(product, "maiores_descontos")
                print(f"[enviado] maiores_descontos: {product.name} | {product.discount_percent:.1f}% OFF")

    def _queued_send(self, group: str, message: str, image_url: str | None = None) -> bool:
        if self._last_send_at is not None:
            elapsed = time.monotonic() - self._last_send_at
            wait = max(0.0, settings.whatsapp_send_delay_seconds - elapsed)
            if wait > 0:
                print(f"[fila] aguardando {wait:.1f}s para proximo envio")
                time.sleep(wait)
        ok = self.whatsapp.send(group, message, image_url=image_url)
        self._last_send_at = time.monotonic()
        if ok:
            self._messages_sent_this_run += 1
        return ok

    def _message_limit_reached(self) -> bool:
        limit = settings.max_messages_per_run
        return limit > 0 and self._messages_sent_this_run >= limit
