from dataclasses import dataclass


@dataclass
class Product:
    product_id: str
    item_id: str | None
    name: str
    category_id: str | None
    domain_id: str | None
    price: float | None
    original_price: float | None
    currency_id: str
    permalink: str
    picture: str | None = None
    # Quando o produto vem do Auvello Admin, o grupo escolhido pelo dev
    # prevalece sobre a classificacao automatica. Fontes normais deixam None.
    forced_group: str | None = None
    discovery_source: str | None = None
    # Mantém as chaves de histórico/cooldown separadas e permite escolher o
    # gerador de link correto. Os objetos antigos continuam sendo Mercado Livre.
    marketplace: str = "mercado_livre"

    @property
    def discount_percent(self) -> float:
        if not self.price or not self.original_price or self.original_price <= self.price:
            return 0.0
        return round((1 - self.price / self.original_price) * 100, 2)
