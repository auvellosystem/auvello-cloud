from app.affiliate import AffiliateClient

url = input("Cole uma URL normal de produto do Mercado Livre: ").strip()
print(AffiliateClient().build(url))
