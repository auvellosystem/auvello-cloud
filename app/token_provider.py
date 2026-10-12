"""Chamado apenas pelo endpoint autenticado no servico antigo."""
import json
from .auth import get_valid_access_token, load_tokens
from .config import settings

if __name__ == "__main__":
    if not settings.database_url:
        raise RuntimeError("O provedor exige o banco PostgreSQL existente.")
    token = get_valid_access_token(allow_interactive=False)
    stored = load_tokens()
    print(json.dumps({"access_token": token, "expires_at": stored.get("expires_at", 0)}))
