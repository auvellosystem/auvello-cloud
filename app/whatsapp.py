from __future__ import annotations

import time
import requests

from .config import settings


class WhatsAppClient:
    def send(self, group_key: str, message: str, image_url: str | None = None) -> bool:
        group_id = settings.group_ids.get(group_key)
        if not group_id:
            print(f"[whatsapp] grupo nao configurado: {group_key}")
            return False

        max_retries = max(0, settings.whatsapp_max_retries)
        payload = {"groupId": group_id, "message": message}
        if image_url:
            payload["imageUrl"] = image_url

        for attempt in range(max_retries + 1):
            try:
                response = requests.post(
                    f"{settings.whatsapp_service_url}/send",
                    json=payload,
                    timeout=settings.request_timeout,
                )
            except requests.RequestException as exc:
                print(f"[whatsapp] falha de conexao: {exc}")
                return False

            if response.ok:
                return True

            text = response.text[:500]
            is_rate_limit = (
                response.status_code == 429
                or "rate-overlimit" in text.lower()
                or "rate limit" in text.lower()
            )
            if is_rate_limit and attempt < max_retries:
                wait = max(1.0, settings.whatsapp_rate_limit_retry_seconds) * (attempt + 1)
                print(f"[whatsapp] limite de envio; aguardando {wait:.0f}s antes da tentativa {attempt + 2}/{max_retries + 1}")
                time.sleep(wait)
                continue

            print(f"[whatsapp] erro {response.status_code}: {text[:300]}")
            return False
        return False
