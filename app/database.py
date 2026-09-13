from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from .config import settings
from .models import Product


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
                """
                CREATE INDEX IF NOT EXISTS idx_price_history_product
                ON price_history(product_key, captured_at)
                """,
                """
                CREATE TABLE IF NOT EXISTS notifications (
                    id BIGSERIAL PRIMARY KEY,
                    product_key TEXT NOT NULL,
                    group_key TEXT NOT NULL,
                    price DOUBLE PRECISION NOT NULL,
                    discount_percent DOUBLE PRECISION NOT NULL,
                    sent_at TIMESTAMPTZ NOT NULL
                )
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_notifications_product
                ON notifications(product_key, group_key, sent_at)
                """,
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
                """
                CREATE INDEX IF NOT EXISTS idx_product_notifications
                ON product_notifications(catalog_product_key, group_key, sent_at)
                """,
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
                """
                CREATE INDEX IF NOT EXISTS idx_price_history_product
                ON price_history(product_key, captured_at)
                """,
                """
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_key TEXT NOT NULL,
                    group_key TEXT NOT NULL,
                    price REAL NOT NULL,
                    discount_percent REAL NOT NULL,
                    sent_at TEXT NOT NULL
                )
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_notifications_product
                ON notifications(product_key, group_key, sent_at)
                """,
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
                """
                CREATE INDEX IF NOT EXISTS idx_product_notifications
                ON product_notifications(catalog_product_key, group_key, sent_at)
                """,
            ]

        with self._connect() as conn:
            for sql in statements:
                conn.execute(sql)

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

    def can_notify(self, product: Product, group_key: str) -> bool:
        since = datetime.now(timezone.utc) - timedelta(hours=settings.cooldown_hours)
        if not self.is_postgres:
            since = since.isoformat()

        p = self._ph
        sql = f"""
            SELECT 1 FROM product_notifications
            WHERE catalog_product_key = {p}
              AND group_key = {p}
              AND sent_at >= {p}
            LIMIT 1
        """
        with self._connect() as conn:
            row = conn.execute(
                sql,
                (self.notification_key(product), group_key, since),
            ).fetchone()
        return row is None

    def mark_notified(self, product: Product, group_key: str) -> None:
        sent_at = datetime.now(timezone.utc)
        if not self.is_postgres:
            sent_at = sent_at.isoformat()

        p = self._ph
        sql = f"""
            INSERT INTO product_notifications
            (catalog_product_key, item_key, group_key, price, discount_percent, sent_at)
            VALUES ({p}, {p}, {p}, {p}, {p}, {p})
        """
        with self._connect() as conn:
            conn.execute(
                sql,
                (
                    self.notification_key(product),
                    product.item_id,
                    group_key,
                    product.price or 0,
                    product.discount_percent,
                    sent_at,
                ),
            )
