from __future__ import annotations

import unicodedata

from .mercado_livre import MercadoLivreClient
from .models import Product

ROOT_TO_GROUP = {
    "MLB1000": "eletronicos_tecnologia",
    "MLB1648": "eletronicos_tecnologia",
    "MLB1430": "moda_vestuario",
    "MLB1051": "celulares_acessorios",
    "MLB1144": "games_acessorios",
    "MLB1574": "utilidades_domesticas",
    "MLB5726": "utilidades_domesticas",
    "MLB1071": "pet_shop",
}

DOMAIN_HINTS = {
    "CELLPHONES": "celulares_acessorios",
    "MOBILE_DEVICE_ACCESSORIES": "celulares_acessorios",
    "GAME_CONSOLES": "games_acessorios",
    "VIDEO_GAMES": "games_acessorios",
    "VIDEO_GAME_ACCESSORIES": "games_acessorios",
    "COMPUTERS": "eletronicos_tecnologia",
    "NOTEBOOKS": "eletronicos_tecnologia",
    "TELEVISIONS": "eletronicos_tecnologia",
    "HEADPHONES": "eletronicos_tecnologia",
    "HOME_APPLIANCES": "utilidades_domesticas",
    "SNEAKERS": "moda_vestuario",
    "CLOTHING": "moda_vestuario",
}

TITLE_GROUP_HINTS = {
    "celulares_acessorios": (
        "smartphone", "celular", "iphone", "galaxy", "motorola", "xiaomi",
        "capa celular", "carregador celular", "pelicula celular",
    ),
    "games_acessorios": (
        "playstation", "ps5", "ps4", "xbox", "nintendo", "videogame",
        "video game", "console", "controle gamer", "jogo gamer",
    ),
    "pet_shop": (
        "racao", "pet shop", "cachorro", "gato", "caes", "gatos",
        "areia sanitaria", "tapete higienico",
    ),
    "utilidades_domesticas": (
        "air fryer", "fritadeira", "aspirador", "liquidificador", "cafeteira",
        "panela", "microondas", "geladeira", "maquina de lavar", "cozinha",
    ),
    "moda_vestuario": (
        "tenis", "camiseta", "camisa", "calca", "vestido", "sandalia",
        "sapato", "jaqueta", "moletom", "bolsa feminina",
    ),
    "eletronicos_tecnologia": (
        "notebook", "computador", "monitor", "smart tv", "televisao", "tablet",
        "fone", "headphone", "impressora", "camera", "ssd", "roteador",
    ),
}


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    return "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()


class Classifier:
    def __init__(self, ml: MercadoLivreClient) -> None:
        self.ml = ml
        self._cache: dict[str, str | None] = {}

    def classify(self, product: Product) -> str | None:
        if product.marketplace == "mercado_livre" and product.category_id:
            if product.category_id in self._cache:
                return self._cache[product.category_id]
            try:
                category = self.ml.get_category(product.category_id)
                ids = [x.get("id") for x in category.get("path_from_root", []) if x.get("id")]
                if product.category_id not in ids:
                    ids.append(product.category_id)
                for category_id in ids:
                    if category_id in ROOT_TO_GROUP:
                        group = ROOT_TO_GROUP[category_id]
                        self._cache[product.category_id] = group
                        return group
            except Exception:
                pass

        domain = (product.domain_id or "").upper().split("-", 1)[-1]
        for hint, group in DOMAIN_HINTS.items():
            if hint in domain:
                return group

        title = _normalize(product.name)
        for group, hints in TITLE_GROUP_HINTS.items():
            if any(hint in title for hint in hints):
                return group

        if product.category_id:
            self._cache[product.category_id] = None
        return None
