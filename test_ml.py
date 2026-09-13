from app.mercado_livre import MercadoLivreClient

ml = MercadoLivreClient()

tests = [
    ("products/search", lambda: ml.search_products(q="PlayStation 5 Slim", limit=2)),
    ("trends", lambda: ml.trends()[:2]),
    ("category", lambda: ml.get_category("MLB1144")),
]

for name, fn in tests:
    print("\n" + "=" * 70)
    print(name)
    try:
        print(fn())
    except Exception as exc:
        print("ERRO:", exc)
