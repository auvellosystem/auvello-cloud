from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlencode

import requests

from .config import settings

AUTH_URL = "https://auth.mercadolivre.com.br/authorization"
TOKEN_URL = "https://api.mercadolibre.com/oauth/token"
TOKEN_PROVIDER = "mercado_livre"


def _store_path() -> Path:
    return Path(settings.token_store)


def _ensure_neon_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS oauth_tokens (
            provider TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def _load_tokens_neon() -> dict:
    import psycopg

    with psycopg.connect(settings.database_url) as conn:
        _ensure_neon_table(conn)
        row = conn.execute(
            "SELECT payload FROM oauth_tokens WHERE provider = %s",
            (TOKEN_PROVIDER,),
        ).fetchone()
        conn.commit()

    if not row:
        return {}
    return json.loads(row[0])


def _save_tokens_neon(data: dict) -> None:
    import psycopg

    payload = json.dumps(data, ensure_ascii=False)
    with psycopg.connect(settings.database_url) as conn:
        _ensure_neon_table(conn)
        conn.execute(
            """
            INSERT INTO oauth_tokens (provider, payload, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (provider)
            DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW()
            """,
            (TOKEN_PROVIDER, payload),
        )
        conn.commit()


def load_tokens() -> dict:
    if settings.database_url:
        return _load_tokens_neon()

    path = _store_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_tokens(data: dict) -> None:
    data = dict(data)
    if data.get("expires_in"):
        data["expires_at"] = int(time.time()) + int(data["expires_in"]) - 60

    if settings.database_url:
        _save_tokens_neon(data)
        return

    _store_path().write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def authorize_interactively() -> dict:
    if not settings.client_id or not settings.client_secret or not settings.redirect_uri:
        raise RuntimeError("Configure ML_CLIENT_ID, ML_CLIENT_SECRET e ML_REDIRECT_URI no .env.")

    verifier, challenge = _pkce_pair()
    params = {
        "response_type": "code",
        "client_id": settings.client_id,
        "redirect_uri": settings.redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = f"{AUTH_URL}?{urlencode(params)}"
    print("\nAbra esta URL e autorize o aplicativo:\n")
    print(url)
    try:
        webbrowser.open(url)
    except Exception:
        pass

    code = input("\nCole aqui o parametro 'code' recebido no redirect: ").strip()
    if not code:
        raise RuntimeError("Codigo de autorizacao vazio.")

    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": settings.client_id,
            "client_secret": settings.client_secret,
            "code": code,
            "redirect_uri": settings.redirect_uri,
            "code_verifier": verifier,
        },
        timeout=settings.request_timeout,
    )
    response.raise_for_status()
    tokens = response.json()
    save_tokens(tokens)
    print("[auth] tokens do Mercado Livre salvos no armazenamento persistente.")
    return tokens


def refresh_access_token(refresh_token: str) -> dict:
    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": settings.client_id,
            "client_secret": settings.client_secret,
            "refresh_token": refresh_token,
        },
        timeout=settings.request_timeout,
    )
    response.raise_for_status()
    tokens = response.json()
    save_tokens(tokens)
    return tokens


def get_valid_access_token() -> str:
    tokens = load_tokens()
    if not tokens:
        tokens = authorize_interactively()

    access_token = tokens.get("access_token")
    expires_at = int(tokens.get("expires_at", 0))
    if access_token and time.time() < expires_at:
        return access_token

    refresh_token = tokens.get("refresh_token")
    if refresh_token:
        tokens = refresh_access_token(refresh_token)
        if tokens.get("access_token"):
            return tokens["access_token"]

    tokens = authorize_interactively()
    return tokens["access_token"]
