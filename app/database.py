from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from .config import settings
from .models import Product


DEFAULT_CATEGORY_NAMES = {
    "eletronicos_tecnologia": "Eletrônicos e Tecnologia",
    "moda_vestuario": "Moda e Vestuário",
    "celulares_acessorios": "Celulares e Acessórios",
    "games_acessorios": "Games e Acessórios",
    "utilidades_domesticas": "Utilidades Domésticas",
    "pet_shop": "Pet Shop",
}


class Database:
    """Banco do Auvello.

    - Se DATABASE_URL estiver preenchida, usa PostgreSQL/Neon.
    - Se DATABASE_URL estiver vazia, mantém compatibilidade com SQLite local.

    As conexões são curtas (abre/usa/fecha a cada operação), o que deixa o
    serviço mais resistente a reinícios/pausas de compute do Neon.
    """

    def __init__(self) -> None:
        self.is_postgres = bool(settings.database_url)
        self._init()
        self._seed_default_categories()
        backend = "PostgreSQL/Neon" if self.is_postgres else f"SQLite ({settings.database_path})"
        print(f"[database] usando {backend}")

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        if self.is_postgres:
            import psycopg
            from psycopg.rows import dict_row
            conn = psycopg.connect(settings.database_url, row_factory=dict_row)
        else:
            conn = sqlite3.connect(settings.database_path)
            conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @property
    def _ph(self) -> str:
        """Placeholder SQL do backend atual."""
        return "%s" if self.is_postgres else "?"

    def _init(self) -> None:
        if self.is_postgres:
            statements = [
                """
                CREATE TABLE IF NOT EXISTS price_history (
                    id BIGSERIAL PRIMARY KEY,
                    product_key TEXT NOT NULL,
                    name TEXT NOT NULL,
                    price DOUBLE PRECISION NOT NULL,
                    original_price DOUBLE PRECISION,
                    discount_percent DOUBLE PRECISION NOT NULL DEFAULT 0,
                    captured_at TIMESTAMPTZ NOT NULL
                )
                """,
                """CREATE INDEX IF NOT EXISTS idx_price_history_product ON price_history(product_key, captured_at)""",
                """
                CREATE TABLE IF NOT EXISTS notifications (
                    id BIGSERIAL PRIMARY KEY,
                    product_key TEXT NOT NULL,
                    group_key TEXT NOT NULL,
                    price DOUBLE PRECISION NOT NULL,
                    discount_percent DOUBLE PRECISION NOT NULL,
                    source_group_key TEXT,
                    variety_key TEXT,
                    product_name TEXT,
                    sent_at TIMESTAMPTZ NOT NULL
                )
                """,
                """CREATE INDEX IF NOT EXISTS idx_notifications_product ON notifications(product_key, group_key, sent_at)""",
                """
                CREATE TABLE IF NOT EXISTS product_notifications (
                    id BIGSERIAL PRIMARY KEY,
                    catalog_product_key TEXT NOT NULL,
                    item_key TEXT,
                    group_key TEXT NOT NULL,
                    price DOUBLE PRECISION NOT NULL,
                    discount_percent DOUBLE PRECISION NOT NULL,
                    sent_at TIMESTAMPTZ NOT NULL
                )
                """,
                """CREATE INDEX IF NOT EXISTS idx_product_notifications ON product_notifications(catalog_product_key, group_key, sent_at)""",
                """
                CREATE TABLE IF NOT EXISTS admin_monitored_products (
                    id BIGSERIAL PRIMARY KEY,
                    product_id TEXT NOT NULL UNIQUE,
                    group_key TEXT NOT NULL,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """,
                """CREATE INDEX IF NOT EXISTS idx_admin_monitored_products_active ON admin_monitored_products(active, group_key)""",
                """
                CREATE TABLE IF NOT EXISTS auvello_categories (
                    id BIGSERIAL PRIMARY KEY,
                    group_key TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    whatsapp_group_id TEXT,
                    search_term TEXT,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    public_visible BOOLEAN NOT NULL DEFAULT TRUE,
                    mirror_to_general BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """,
                """CREATE INDEX IF NOT EXISTS idx_auvello_categories_active ON auvello_categories(active, public_visible)""",
                """
                CREATE TABLE IF NOT EXISTS offer_candidates (
                    catalog_product_key TEXT PRIMARY KEY,
                    product_id TEXT,
                    item_id TEXT,
                    name TEXT NOT NULL,
                    category_id TEXT,
                    domain_id TEXT,
                    price DOUBLE PRECISION NOT NULL,
                    original_price DOUBLE PRECISION,
                    currency_id TEXT NOT NULL,
                    permalink TEXT NOT NULL,
                    picture TEXT,
                    forced_group TEXT,
                    discovery_source TEXT,
                    marketplace TEXT NOT NULL DEFAULT 'mercado_livre',
                    group_key TEXT NOT NULL,
                    previous_price DOUBLE PRECISION,
                    drop_percent DOUBLE PRECISION NOT NULL DEFAULT 0,
                    score DOUBLE PRECISION NOT NULL DEFAULT 0,
                    updated_at TIMESTAMPTZ NOT NULL
                )
                """,
                """CREATE INDEX IF NOT EXISTS idx_offer_candidates_group_updated ON offer_candidates(group_key, updated_at)""",
                """
                CREATE TABLE IF NOT EXISTS community_requests (
                    id BIGSERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    whatsapp TEXT NOT NULL,
                    reference_url TEXT NOT NULL,
                    reference_product_id TEXT,
                    desired_item TEXT NOT NULL,
                    suggested_group_key TEXT NOT NULL,
                    approved_group_key TEXT,
                    approved_search_term TEXT,
                    notes TEXT,
                    status TEXT NOT NULL DEFAULT 'pendente',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """,
                """CREATE INDEX IF NOT EXISTS idx_community_requests_status ON community_requests(status, approved_group_key, created_at)""",
            ]
        else:
            statements = [
                """
                CREATE TABLE IF NOT EXISTS price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_key TEXT NOT NULL,
                    name TEXT NOT NULL,
                    price REAL NOT NULL,
                    original_price REAL,
                    discount_percent REAL NOT NULL DEFAULT 0,
                    captured_at TEXT NOT NULL
                )
                """,
                """CREATE INDEX IF NOT EXISTS idx_price_history_product ON price_history(product_key, captured_at)""",
                """
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_key TEXT NOT NULL,
                    group_key TEXT NOT NULL,
                    price REAL NOT NULL,
                    discount_percent REAL NOT NULL,
                    source_group_key TEXT,
                    variety_key TEXT,
                    product_name TEXT,
                    sent_at TEXT NOT NULL
                )
                """,
                """CREATE INDEX IF NOT EXISTS idx_notifications_product ON notifications(product_key, group_key, sent_at)""",
                """
                CREATE TABLE IF NOT EXISTS product_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    catalog_product_key TEXT NOT NULL,
                    item_key TEXT,
                    group_key TEXT NOT NULL,
                    price REAL NOT NULL,
                    discount_percent REAL NOT NULL,
                    sent_at TEXT NOT NULL
                )
                """,
                """CREATE INDEX IF NOT EXISTS idx_product_notifications ON product_notifications(catalog_product_key, group_key, sent_at)""",
                """
                CREATE TABLE IF NOT EXISTS admin_monitored_products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id TEXT NOT NULL UNIQUE,
                    group_key TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """,
                """CREATE INDEX IF NOT EXISTS idx_admin_monitored_products_active ON admin_monitored_products(active, group_key)""",
                """
                CREATE TABLE IF NOT EXISTS auvello_categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_key TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    whatsapp_group_id TEXT,
                    search_term TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    public_visible INTEGER NOT NULL DEFAULT 1,
                    mirror_to_general INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """,
                """CREATE INDEX IF NOT EXISTS idx_auvello_categories_active ON auvello_categories(active, public_visible)""",
                """
                CREATE TABLE IF NOT EXISTS offer_candidates (
                    catalog_product_key TEXT PRIMARY KEY,
                    product_id TEXT,
                    item_id TEXT,
                    name TEXT NOT NULL,
                    category_id TEXT,
                    domain_id TEXT,
                    price REAL NOT NULL,
                    original_price REAL,
                    currency_id TEXT NOT NULL,
                    permalink TEXT NOT NULL,
                    picture TEXT,
                    forced_group TEXT,
                    discovery_source TEXT,
                    marketplace TEXT NOT NULL DEFAULT 'mercado_livre',
                    group_key TEXT NOT NULL,
                    previous_price REAL,
                    drop_percent REAL NOT NULL DEFAULT 0,
                    score REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """,
                """CREATE INDEX IF NOT EXISTS idx_offer_candidates_group_updated ON offer_candidates(group_key, updated_at)""",
                """
                CREATE TABLE IF NOT EXISTS community_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    whatsapp TEXT NOT NULL,
                    reference_url TEXT NOT NULL,
                    reference_product_id TEXT,
                    desired_item TEXT NOT NULL,
                    suggested_group_key TEXT NOT NULL,
                    approved_group_key TEXT,
                    approved_search_term TEXT,
                    notes TEXT,
                    status TEXT NOT NULL DEFAULT 'pendente',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """,
                """CREATE INDEX IF NOT EXISTS idx_community_requests_status ON community_requests(status, approved_group_key, created_at)""",
            ]

        with self._connect() as conn:
            for sql in statements:
                conn.execute(sql)

            if self.is_postgres:
                conn.execute("ALTER TABLE product_notifications ADD COLUMN IF NOT EXISTS source_group_key TEXT")
                conn.execute("ALTER TABLE product_notifications ADD COLUMN IF NOT EXISTS variety_key TEXT")
                conn.execute("ALTER TABLE product_notifications ADD COLUMN IF NOT EXISTS product_name TEXT")
                conn.execute("ALTER TABLE offer_candidates ADD COLUMN IF NOT EXISTS marketplace TEXT NOT NULL DEFAULT 'mercado_livre'")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_product_notifications_general_source ON product_notifications(group_key, source_group_key, sent_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_product_notifications_general_variety ON product_notifications(group_key, variety_key, sent_at)")
            else:
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(product_notifications)").fetchall()}
                if "source_group_key" not in columns:
                    conn.execute("ALTER TABLE product_notifications ADD COLUMN source_group_key TEXT")
                if "variety_key" not in columns:
                    conn.execute("ALTER TABLE product_notifications ADD COLUMN variety_key TEXT")
                if "product_name" not in columns:
                    conn.execute("ALTER TABLE product_notifications ADD COLUMN product_name TEXT")
                offer_columns = {row["name"] for row in conn.execute("PRAGMA table_info(offer_candidates)").fetchall()}
                if "marketplace" not in offer_columns:
                    conn.execute("ALTER TABLE offer_candidates ADD COLUMN marketplace TEXT NOT NULL DEFAULT 'mercado_livre'")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_product_notifications_general_source ON product_notifications(group_key, source_group_key, sent_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_product_notifications_general_variety ON product_notifications(group_key, variety_key, sent_at)")

    @staticmethod
    def price_key(product: Product) -> str:
        """Histórico é por oferta/item, porque cada vendedor tem seu preço."""
        return product.item_id or product.product_id

    @staticmethod
    def notification_key(product: Product) -> str:
        """Publicação/cooldown é por PRODUCT de catálogo."""
        return product.product_id or product.item_id or ""

    @staticmethod
    def key(product: Product) -> str:
        return Database.price_key(product)

    def record_price(self, product: Product) -> None:
        if product.price is None:
            return

        p = self._ph
        sql = f"""
            INSERT INTO price_history
            (product_key, name, price, original_price, discount_percent, captured_at)
            VALUES ({p}, {p}, {p}, {p}, {p}, {p})
        """
        captured_at = datetime.now(timezone.utc)
        if not self.is_postgres:
            captured_at = captured_at.isoformat()

        with self._connect() as conn:
            conn.execute(
                sql,
                (
                    self.price_key(product),
                    product.name,
                    product.price,
                    product.original_price,
                    product.discount_percent,
                    captured_at,
                ),
            )

    def previous_price(self, product: Product) -> float | None:
        p = self._ph
        sql = f"""
            SELECT price FROM price_history
            WHERE product_key = {p}
            ORDER BY id DESC
            LIMIT 2
        """
        with self._connect() as conn:
            rows = conn.execute(sql, (self.price_key(product),)).fetchall()

        if len(rows) < 2:
            return None
        return float(rows[1]["price"])

    def last_notification(self, product: Product, group_key: str) -> dict[str, Any] | None:
        """Última publicação confirmada deste PRODUCT_ID neste grupo."""
        p = self._ph
        sql = f"""
            SELECT price, discount_percent, sent_at
            FROM product_notifications
            WHERE catalog_product_key = {p}
              AND group_key = {p}
            ORDER BY sent_at DESC
            LIMIT 1
        """
        with self._connect() as conn:
            row = conn.execute(
                sql,
                (self.notification_key(product), group_key),
            ).fetchone()
        if row is None:
            return None
        return dict(row)

    def can_notify_fixed_product(self, product: Product, group_key: str) -> bool:
        """Regra especial dos produtos fixados no Admin.

        - Primeira publicação: liberada normalmente.
        - Oferta melhor que a última publicada: libera imediatamente.
        - Oferta exatamente igual: pode repetir após o cooldown específico dos fixados.
        - Oferta pior: não repete.

        Uma oferta é considerada melhor quando o preço atual diminuiu OU o
        percentual de desconto aumentou em relação à última publicação.
        """
        last = self.last_notification(product, group_key)
        if last is None:
            return True

        current_price = float(product.price or 0.0)
        current_discount = float(product.discount_percent or 0.0)
        last_price = float(last.get("price") or 0.0)
        last_discount = float(last.get("discount_percent") or 0.0)

        price_better = current_price > 0 and last_price > 0 and current_price < (last_price - 0.009)
        discount_better = current_discount > (last_discount + 0.009)

        # Qualquer melhora relevante libera o produto imediatamente, mesmo dentro das 12h.
        if price_better or discount_better:
            return True

        same_price = abs(current_price - last_price) < 0.01
        same_discount = abs(current_discount - last_discount) < 0.01

        # Oferta pior (ou diferente sem nenhuma melhora) nunca é repetida.
        if not (same_price and same_discount):
            return False

        # Oferta exatamente igual só pode repetir depois do intervalo configurado.
        sent_at = last.get("sent_at")
        if isinstance(sent_at, str):
            try:
                sent_at = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
            except ValueError:
                return False
        if sent_at is None:
            return False
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)

        min_age = timedelta(hours=max(0, settings.fixed_product_cooldown_hours))
        return datetime.now(timezone.utc) - sent_at >= min_age

    def notification_price_drop_percent(self, product: Product, group_key: str) -> float:
        last = self.last_notification(product, group_key)
        if not last:
            return 0.0
        last_price = float(last.get("price") or 0.0)
        current_price = float(product.price or 0.0)
        if last_price <= 0 or current_price <= 0 or current_price >= last_price:
            return 0.0
        return (1.0 - current_price / last_price) * 100.0

    def is_price_drop_exception(self, product: Product, group_key: str) -> bool:
        return self.notification_price_drop_percent(product, group_key) >= settings.min_price_drop_percent

    def can_notify(self, product: Product, group_key: str) -> bool:
        last = self.last_notification(product, group_key)
        if last is None:
            return True
        if self.is_price_drop_exception(product, group_key):
            return True
        hours = settings.general_product_cooldown_hours if group_key == "geral" else settings.specific_product_cooldown_hours
        sent_at = last.get("sent_at")
        if isinstance(sent_at, str):
            try:
                sent_at = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
            except ValueError:
                return False
        if sent_at is None:
            return False
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - sent_at >= timedelta(hours=max(0, hours))

    def recent_general_source_groups(self, minutes: int | None = None) -> set[str]:
        window = max(1, int(minutes if minutes is not None else settings.general_category_cooldown_minutes))
        since = datetime.now(timezone.utc) - timedelta(minutes=window)
        if not self.is_postgres:
            since = since.isoformat()
        p = self._ph
        sql = f"""
            SELECT DISTINCT source_group_key
            FROM product_notifications
            WHERE group_key = {p}
              AND source_group_key IS NOT NULL
              AND source_group_key <> ''
              AND sent_at >= {p}
        """
        with self._connect() as conn:
            rows = conn.execute(sql, ("geral", since)).fetchall()
        return {str(row["source_group_key"]) for row in rows if row["source_group_key"]}

    def recent_general_variety_keys(self, hours: int | None = None) -> set[str]:
        window = max(1, int(hours if hours is not None else settings.general_type_cooldown_hours))
        since = datetime.now(timezone.utc) - timedelta(hours=window)
        if not self.is_postgres:
            since = since.isoformat()
        p = self._ph
        sql = f"""
            SELECT DISTINCT variety_key
            FROM product_notifications
            WHERE group_key = {p}
              AND variety_key IS NOT NULL
              AND variety_key <> ''
              AND sent_at >= {p}
        """
        with self._connect() as conn:
            rows = conn.execute(sql, ("geral", since)).fetchall()
        return {str(row["variety_key"]) for row in rows if row["variety_key"]}

    def mark_notified(self, product: Product, group_key: str, *, source_group_key: str | None = None, variety_key: str | None = None) -> None:
        sent_at = datetime.now(timezone.utc)
        if not self.is_postgres:
            sent_at = sent_at.isoformat()
        p = self._ph
        sql = f"""
            INSERT INTO product_notifications
            (catalog_product_key, item_key, group_key, price, discount_percent, source_group_key, variety_key, product_name, sent_at)
            VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p})
        """
        with self._connect() as conn:
            conn.execute(sql, (self.notification_key(product), product.item_id, group_key, product.price or 0, product.discount_percent, source_group_key, variety_key, product.name, sent_at))

    def count_group_notifications_since(self, group_key: str, minutes: int = 60) -> int:
        """Conta envios confirmados a um grupo dentro de uma janela móvel.

        É usado como teto de volume do Geral. Como mark_notified só é chamado
        após o WhatsApp confirmar o envio, falhas não consomem a cota.
        """
        since = datetime.now(timezone.utc) - timedelta(minutes=max(1, minutes))
        if not self.is_postgres:
            since = since.isoformat()

        p = self._ph
        sql = f"""
            SELECT COUNT(*) AS total
            FROM product_notifications
            WHERE group_key = {p}
              AND sent_at >= {p}
        """
        with self._connect() as conn:
            row = conn.execute(sql, (group_key, since)).fetchone()
        if not row:
            return 0
        return int(row["total"] or 0)

    def _seed_default_categories(self) -> None:
        """Garante as seis categorias históricas sem sobrescrever ajustes do Admin."""
        p = self._ph
        sql = f"""
            INSERT INTO auvello_categories (
                group_key, name, whatsapp_group_id, search_term, active,
                public_visible, mirror_to_general, created_at, updated_at
            ) VALUES ({p}, {p}, {p}, NULL, {p}, {p}, {p}, {p}, {p})
            ON CONFLICT(group_key) DO NOTHING
        """
        now = datetime.now(timezone.utc)
        stored_now = now if self.is_postgres else now.isoformat()
        truthy = True if self.is_postgres else 1
        with self._connect() as conn:
            for group_key, name in DEFAULT_CATEGORY_NAMES.items():
                group_id = settings.group_ids.get(group_key) or None
                conn.execute(sql, (
                    group_key, name, group_id, truthy, truthy, truthy,
                    stored_now, stored_now,
                ))

    def active_categories(self) -> list[dict[str, Any]]:
        """Categorias específicas gerenciadas pelo Admin."""
        p = self._ph
        active_value = True if self.is_postgres else 1
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT id, group_key, name, whatsapp_group_id, search_term,
                           active, public_visible, mirror_to_general, created_at, updated_at
                    FROM auvello_categories
                    WHERE active = {p}
                    ORDER BY name ASC""",
                (active_value,),
            ).fetchall()
        return [dict(row) for row in rows]

    def active_search_categories(self) -> list[dict[str, str]]:
        """Categorias com termo próprio de descoberta automática."""
        return [
            {
                "group_key": str(row["group_key"]),
                "name": str(row["name"]),
                "search_term": str(row["search_term"] or "").strip(),
            }
            for row in self.active_categories()
            if str(row.get("search_term") or "").strip()
        ]

    def category_group_id(self, group_key: str) -> str | None:
        """Resolve o JID do WhatsApp salvo no Admin, com fallback para .env."""
        p = self._ph
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT whatsapp_group_id FROM auvello_categories WHERE group_key = {p} AND active = {p} LIMIT 1",
                (group_key, True if self.is_postgres else 1),
            ).fetchone()
        if row and row["whatsapp_group_id"]:
            return str(row["whatsapp_group_id"]).strip() or None
        return settings.group_ids.get(group_key) or None

    def category_mirrors_to_general(self, group_key: str) -> bool:
        p = self._ph
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT mirror_to_general FROM auvello_categories WHERE group_key = {p} LIMIT 1",
                (group_key,),
            ).fetchone()
        if row is None:
            return True
        return bool(row["mirror_to_general"])

    def upsert_offer_candidates(self, candidates: list[dict[str, Any]]) -> int:
        """Atualiza o cache persistente de candidatos elegíveis.

        O cache é compartilhado pelos grupos específicos e pelo Geral. Uma
        descoberta nova sobrescreve os dados do mesmo PRODUCT_ID, sem depender
        da memória do processo (importante para reinícios do Render).
        """
        if not candidates:
            return 0

        p = self._ph
        now = datetime.now(timezone.utc)
        stored_now = now if self.is_postgres else now.isoformat()
        sql = f"""
            INSERT INTO offer_candidates (
                catalog_product_key, product_id, item_id, name, category_id,
                domain_id, price, original_price, currency_id, permalink,
                picture, forced_group, discovery_source, marketplace, group_key,
                previous_price, drop_percent, score, updated_at
            ) VALUES (
                {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p},
                {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}
            )
            ON CONFLICT(catalog_product_key) DO UPDATE SET
                product_id = excluded.product_id,
                item_id = excluded.item_id,
                name = excluded.name,
                category_id = excluded.category_id,
                domain_id = excluded.domain_id,
                price = excluded.price,
                original_price = excluded.original_price,
                currency_id = excluded.currency_id,
                permalink = excluded.permalink,
                picture = excluded.picture,
                forced_group = excluded.forced_group,
                discovery_source = excluded.discovery_source,
                marketplace = excluded.marketplace,
                group_key = excluded.group_key,
                previous_price = excluded.previous_price,
                drop_percent = excluded.drop_percent,
                score = excluded.score,
                updated_at = excluded.updated_at
        """
        with self._connect() as conn:
            for c in candidates:
                conn.execute(sql, (
                    c["catalog_product_key"], c.get("product_id"), c.get("item_id"),
                    c["name"], c.get("category_id"), c.get("domain_id"), c["price"],
                    c.get("original_price"), c.get("currency_id") or "BRL",
                    c["permalink"], c.get("picture"), c.get("forced_group"),
                    c.get("discovery_source"), c.get("marketplace") or "mercado_livre",
                    c["group_key"], c.get("previous_price"),
                    c.get("drop_percent", 0.0), c.get("score", 0.0), stored_now,
                ))
        return len(candidates)

    def prune_offer_candidates(self) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.candidate_cache_ttl_minutes)
        stored_cutoff = cutoff if self.is_postgres else cutoff.isoformat()
        p = self._ph
        with self._connect() as conn:
            cur = conn.execute(f"DELETE FROM offer_candidates WHERE updated_at < {p}", (stored_cutoff,))
            return max(0, int(cur.rowcount or 0))

    def offer_candidates(self, group_key: str | None = None) -> list[dict[str, Any]]:
        """Lê somente candidatos ainda frescos no cache."""
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.candidate_cache_ttl_minutes)
        stored_cutoff = cutoff if self.is_postgres else cutoff.isoformat()
        p = self._ph
        params: list[Any] = [stored_cutoff]
        where_group = ""
        if group_key:
            where_group = f" AND group_key = {p}"
            params.append(group_key)
        sql = f"""
            SELECT * FROM offer_candidates
            WHERE updated_at >= {p}{where_group}
            ORDER BY updated_at DESC, score DESC
        """
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def active_admin_products(self) -> list[dict[str, str]]:
        """Produtos fixados pelo dev no Auvello Admin."""
        active_value = True if self.is_postgres else 1
        p = self._ph
        sql = f"""
            SELECT product_id, group_key
            FROM admin_monitored_products
            WHERE active = {p}
            ORDER BY id ASC
        """
        with self._connect() as conn:
            rows = conn.execute(sql, (active_value,)).fetchall()
        return [
            {"product_id": str(row["product_id"]), "group_key": str(row["group_key"])}
            for row in rows
        ]
    def approved_community_requests(self) -> list[dict[str, str]]:
        """Interesses da comunidade já aprovados pelo dev.

        Cada registro representa um termo/categoria de busca, não um anúncio
        específico. O link enviado pelo membro é apenas referência para análise.
        """
        p = self._ph
        sql = f"""
            SELECT id, approved_search_term, approved_group_key
            FROM community_requests
            WHERE status = {p}
              AND approved_search_term IS NOT NULL
              AND approved_group_key IS NOT NULL
            ORDER BY id ASC
        """
        with self._connect() as conn:
            rows = conn.execute(sql, ("aprovado",)).fetchall()
        return [
            {
                "id": str(row["id"]),
                "search_term": str(row["approved_search_term"]),
                "group_key": str(row["approved_group_key"]),
            }
            for row in rows
        ]
