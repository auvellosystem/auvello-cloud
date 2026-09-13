from __future__ import annotations

import requests
from .config import settings


class AffiliateError(RuntimeError):
    pass


class AffiliateClient:
    def build(self, origin_url: str) -> str:
        if settings.affiliate_mode == "disabled":
            return origin_url
        if settings.affiliate_mode != "portal":
            raise AffiliateError(f"AFFILIATE_MODE invalido: {settings.affiliate_mode!r}")
        if not settings.affiliate_tag:
            raise AffiliateError("AFFILIATE_TAG nao configurada.")
        if not settings.affiliate_cookie:
            raise AffiliateError("ML_AFFILIATE_COOKIE nao configurado no .env.")

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": settings.affiliate_user_agent,
            "Cookie": settings.affiliate_cookie,
            "Origin": "https://www.mercadolivre.com.br",
            "Referer": "https://www.mercadolivre.com.br/",
        }
        response = requests.post(
            settings.affiliate_create_url,
            headers=headers,
            json={"urls": [origin_url], "tag": settings.affiliate_tag},
            timeout=settings.request_timeout,
        )
        if not response.ok:
            raise AffiliateError(f"Gerador de afiliados HTTP {response.status_code}: {response.text[:500]}")
        data = response.json()
        urls = data.get("urls") or []
        if not urls or not urls[0].get("short_url"):
            raise AffiliateError(f"Resposta sem short_url: {data}")
        return urls[0]["short_url"]
