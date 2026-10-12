from __future__ import annotations

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


class Classifier:
    def __init__(self, ml: MercadoLivreClient) -> None:
        self.ml = ml
        self._cache: dict[str, str | None] = {}

    def classify(self, product: Product) -> str | None:
        if product.category_id:
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

        if product.category_id:
            self._cache[product.category_id] = None
        return None
