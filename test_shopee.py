from app.config import settings
from app.shopee import ShopeeClient


def main() -> None:
    client = ShopeeClient()
    if not client.configured:
        print("Shopee não configurada. Preencha SHOPEE_APP_ID e SHOPEE_SECRET no .env.")
        return

    offers = client.search_offers("air fryer", limit=5)
    print(f"OK: {len(offers)} ofertas recebidas")
    for offer in offers[:5]:
        print(
            f"- {offer.name} | R$ {offer.price:.2f} | "
            f"{offer.discount_percent:.0f}% OFF | {offer.permalink}"
        )


if __name__ == "__main__":
    main()
