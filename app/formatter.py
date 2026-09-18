from .models import Product


def brl(value: float | None) -> str:
    if value is None:
        return "-"
    text = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {text}"


def build_message(product: Product, affiliate_url: str, previous_price: float | None = None) -> str:
    marketplace = "Shopee" if product.marketplace == "shopee" else "Mercado Livre"
    lines = ["🔥 *ACHADO AUVELLO*", f"🛍️ {marketplace}", "", f"*{product.name}*", ""]
    if product.original_price and product.original_price > (product.price or 0):
        lines.append(f"De: ~{brl(product.original_price)}~")
    lines.append(f"Por: *{brl(product.price)}*")
    if product.discount_percent > 0:
        lines.append(f"💥 *{product.discount_percent:.0f}% OFF*")
    if previous_price and product.price and previous_price > product.price:
        drop = (1 - product.price / previous_price) * 100
        lines.append(f"📉 Caiu {drop:.1f}% desde a ultima consulta ({brl(previous_price)} → {brl(product.price)})")
    lines += ["", "🛒 Link:", affiliate_url]
    return "\n".join(lines)
