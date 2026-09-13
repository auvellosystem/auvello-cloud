from __future__ import annotations

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "sim", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).replace(",", "."))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    site_id: str = os.getenv("ML_SITE_ID", "MLB")
    client_id: str = os.getenv("ML_CLIENT_ID", "")
    client_secret: str = os.getenv("ML_CLIENT_SECRET", "")
    redirect_uri: str = os.getenv("ML_REDIRECT_URI", "")
    token_store: str = os.getenv("ML_TOKEN_STORE", "token_store.json")

    discovery_trends: bool = _bool("DISCOVERY_TRENDS", True)
    discovery_watchlist: bool = _bool("DISCOVERY_WATCHLIST", True)
    max_trend_terms: int = _int("MAX_TREND_TERMS", 20)
    max_products_per_query: int = _int("MAX_PRODUCTS_PER_QUERY", 5)
    request_timeout: int = _int("REQUEST_TIMEOUT", 20)

    min_discount_percent: float = _float("MIN_DISCOUNT_PERCENT", 15)
    big_discount_percent: float = _float("BIG_DISCOUNT_PERCENT", 20)
    min_price_drop_percent: float = _float("MIN_PRICE_DROP_PERCENT", 10)
    cooldown_hours: int = _int("COOLDOWN_HOURS", 24)
    # Scheduler desacoplado: descoberta no Mercado Livre e publicação usam
    # relógios independentes. O grupo Geral reaproveita o cache e não força
    # uma nova busca no Mercado Livre a cada 5 minutos.
    discovery_interval_minutes: int = _int("DISCOVERY_INTERVAL_MINUTES", 10)
    specific_group_interval_minutes: int = _int("SPECIFIC_GROUP_INTERVAL_MINUTES", 10)
    general_group_interval_minutes: int = _int("GENERAL_GROUP_INTERVAL_MINUTES", 5)
    candidate_cache_ttl_minutes: int = _int("CANDIDATE_CACHE_TTL_MINUTES", 30)

    # Controles de publicação/WhatsApp.
    # 12 s = no máximo ~5 disparos/minuto durante uma fila normal.
    whatsapp_send_delay_seconds: float = _float("WHATSAPP_SEND_DELAY_SECONDS", 12)
    whatsapp_rate_limit_retry_seconds: float = _float("WHATSAPP_RATE_LIMIT_RETRY_SECONDS", 30)
    whatsapp_max_retries: int = _int("WHATSAPP_MAX_RETRIES", 2)
    # Airbag de segurança por ciclo de publicação. Como cada envio em grupo
    # específico também pode ser espelhado no Geral, o teto precisa comportar
    # os dois destinos. Use 0 para desabilitar o airbag.
    max_messages_per_cycle: int = _int("MAX_MESSAGES_PER_CYCLE", 70)
    max_products_general: int = _int("MAX_PRODUCTS_GENERAL", 5)

    # Auvello Score: desconto + economia em reais + acessibilidade.
    score_discount_weight: float = _float("SCORE_DISCOUNT_WEIGHT", 0.45)
    score_savings_weight: float = _float("SCORE_SAVINGS_WEIGHT", 0.30)
    score_accessibility_weight: float = _float("SCORE_ACCESSIBILITY_WEIGHT", 0.25)

    # Slots de oportunidade por grupo (máximo 5 produtos).
    max_products_per_group: int = _int("MAX_PRODUCTS_PER_GROUP", 5)
    slot_top_discount_count: int = _int("SLOT_TOP_DISCOUNT_COUNT", 2)
    slot_min_strong_discount: float = _float("SLOT_MIN_STRONG_DISCOUNT", 25)
    slot_min_savings_reais: float = _float("SLOT_MIN_SAVINGS_REAIS", 100)
    slot_accessible_max_price: float = _float("SLOT_ACCESSIBLE_MAX_PRICE", 500)
    slot_accessible_min_discount: float = _float("SLOT_ACCESSIBLE_MIN_DISCOUNT", 25)
    slot_min_score: float = _float("SLOT_MIN_SCORE", 55)

    affiliate_mode: str = os.getenv("AFFILIATE_MODE", "portal").strip().lower()
    affiliate_tag: str = os.getenv("AFFILIATE_TAG", "")
    affiliate_create_url: str = os.getenv(
        "AFFILIATE_CREATE_URL",
        "https://www.mercadolivre.com.br/affiliate-program/api/v2/affiliates/createLink",
    )
    affiliate_cookie: str = os.getenv("ML_AFFILIATE_COOKIE", "")
    affiliate_user_agent: str = os.getenv("ML_AFFILIATE_USER_AGENT", "Mozilla/5.0")

    whatsapp_service_url: str = os.getenv("WHATSAPP_SERVICE_URL", "http://localhost:3000").rstrip("/")
    database_url: str = os.getenv("DATABASE_URL", "").strip()
    database_path: str = os.getenv("DATABASE_PATH", "auvello.db")

    @property
    def group_ids(self) -> dict[str, str]:
        return {
            "eletronicos_tecnologia": os.getenv("WA_GROUP_ELETRONICOS", ""),
            "moda_vestuario": os.getenv("WA_GROUP_MODA", ""),
            "celulares_acessorios": os.getenv("WA_GROUP_CELULARES", ""),
            "games_acessorios": os.getenv("WA_GROUP_GAMES", ""),
            "utilidades_domesticas": os.getenv("WA_GROUP_UTILIDADES", ""),
            "pet_shop": os.getenv("WA_GROUP_PET", ""),
            # O antigo grupo "Maiores Descontos" foi reaproveitado como Geral.
            # WA_GROUP_GERAL é o nome novo; a variável antiga continua aceita
            # para não exigir troca imediata no Render.
            "geral": os.getenv("WA_GROUP_GERAL", "") or os.getenv("WA_GROUP_MAIORES_DESCONTOS", ""),
        }


settings = Settings()
