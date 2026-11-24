"""Encryption at rest (Fernet) for AI keys and connector state, plus token hashing."""
from __future__ import annotations

import hashlib
import secrets
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from scraper0.config import REPO_ROOT, get_settings


class CryptoError(RuntimeError):
    pass


@lru_cache
def _fernet() -> Fernet:
    s = get_settings()
    if s.secret_key is not None:
        return Fernet(s.secret_key.get_secret_value().encode())
    if s.env not in ("local", "test"):
        raise CryptoError("SCRAPER0_SECRET_KEY is required outside local/test (Fernet key: "
                          "python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')")
    # local convenience: a key file next to the local SQLite database
    path = REPO_ROOT / "data" / ".secret_key"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(Fernet.generate_key())
        path.chmod(0o600)
    return Fernet(path.read_bytes().strip())


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise CryptoError("cannot decrypt: wrong SCRAPER0_SECRET_KEY?") from e


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hint(secret: str) -> str:
    return "…" + secret[-4:] if len(secret) >= 8 else "…"
