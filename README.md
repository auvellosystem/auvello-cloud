# Auvello v2

Projeto novo para monitorar ofertas do Mercado Livre, classificar automaticamente os produtos e enviar links de afiliado aos grupos Auvello.

## Fluxo

```text
Trends + Watchlist
       ↓
/products/search
       ↓
Produto do catálogo
       ↓
category_id / domain_id
       ↓
Árvore de categorias do Mercado Livre
       ↓
Grupo Auvello correto
       ↓
Regra de desconto / queda de preço
       ↓
Gerador de link afiliado
       ↓
WhatsApp
```

## Instalação

No PowerShell:

```powershell
cd auvello-v2
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Edite o `.env` e preencha:

- `ML_CLIENT_ID`
- `ML_CLIENT_SECRET`
- `ML_REDIRECT_URI`
- IDs dos grupos `WA_GROUP_*`
- `AFFILIATE_TAG`
- `ML_AFFILIATE_COOKIE` se for usar o gerador do portal

Nunca publique seu `.env`.

## Autorizar o Mercado Livre

```powershell
python main.py --auth
```

## Testar Mercado Livre

```powershell
python test_ml.py
```

O projeto usa o endpoint oficial `/products/search` por nome, Product ID, Part Number ou GTIN/EAN/UPC. Ele não depende da busca ampla `/sites/MLB/search`, que no seu aplicativo está retornando 403.

Também usa `/trends/MLB` para obter termos populares e buscar os produtos correspondentes.

## Classificação automática

O Auvello não precisa de um dicionário com milhares de nomes.

Ele consulta a categoria real do produto e usa `path_from_root` para descobrir a categoria raiz:

- `MLB1000` / `MLB1648` → Eletrônicos/Tecnologia
- `MLB1430` → Moda/Vestuário
- `MLB1051` → Celulares e Acessórios
- `MLB1144` → Games e Acessórios
- `MLB1574` / `MLB5726` → Utilidades Domésticas
- `MLB1071` → Pet Shop

Há também um fallback por `domain_id`.

## Watchlist

Edite `watchlist.json` para adicionar produtos específicos:

```json
{
  "queries": [
    "PlayStation 5 Slim",
    "iPhone 16"
  ],
  "product_identifiers": [
    "7890000000000"
  ]
}
```

## Afiliado

Foi observado no portal do Mercado Livre o endpoint:

```text
POST https://www.mercadolivre.com.br/affiliate-program/api/v2/affiliates/createLink
```

Body observado:

```json
{
  "urls": ["URL_DO_PRODUTO"],
  "tag": "moma4372121"
}
```

A resposta contém `short_url`, como `https://meli.la/...`.

Esse endpoint pertence ao portal web e não aparece documentado como API pública do Developers. Portanto, pode mudar e pode exigir sessão web válida.

Para testar:

1. Abra o DevTools > Network.
2. Gere um link no portal.
3. Clique na requisição `createLink`.
4. Em Request Headers, copie o valor do header `Cookie` da sua própria sessão.
5. Cole somente no seu `.env` em `ML_AFFILIATE_COOKIE`.
6. Se necessário, copie também o `User-Agent` para `ML_AFFILIATE_USER_AGENT`.
7. Rode:

```powershell
python test_affiliate.py
```

Nunca envie seu cookie para outra pessoa.

## WhatsApp

O serviço incluído usa Baileys para conseguir enviar a grupos. Baileys não é a API oficial da Meta e pode quebrar após mudanças do WhatsApp.

```powershell
cd whatsapp-service
npm install
npm start
```

Escaneie o QR Code. Depois acesse:

```text
http://localhost:3000/groups
```

Copie cada ID de grupo para o `.env`.

## Executar uma rodada

```powershell
python main.py --once
```

## Executar continuamente

```powershell
python main.py
```

Por padrão, roda a cada 60 minutos.

## Regras de alerta

```env
MIN_DISCOUNT_PERCENT=15
BIG_DISCOUNT_PERCENT=20
MIN_PRICE_DROP_PERCENT=10
COOLDOWN_HOURS=24
```

O produto vai para o grupo normal quando:

- desconto anunciado >= 15%; ou
- preço caiu >= 10% desde a consulta anterior.

Se o desconto anunciado for >= 20%, também vai para `Auvello - Maiores Descontos`.

## Arquivos principais

- `main.py` → scheduler principal
- `app/auth.py` → OAuth + refresh token
- `app/mercado_livre.py` → APIs Mercado Livre
- `app/discovery.py` → Trends + Watchlist
- `app/classifier.py` → categoria → grupo
- `app/database.py` → histórico de preços/cooldown
- `app/affiliate.py` → geração do meli.la
- `app/whatsapp.py` → chamada ao serviço local
- `app/formatter.py` → mensagem do grupo
- `whatsapp-service/index.js` → envio aos grupos
