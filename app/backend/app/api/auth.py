"""Contas e autenticação.

Dois modos convivem:
- API_TOKEN (variável de ambiente): administrador/uso pessoal — vê tudo, sem cota (compatível com o MVP);
- usuários com e-mail e senha (scrypt) recebem um JWT (HS256) em POST /auth/login.
"""

import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Annotated

import jwt
from fastapi import Header, HTTPException, status

from app.api.db import User, session
from app.core.config import get_settings

SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}
MIN_PASSWORD = 8


@dataclass(frozen=True)
class Principal:
    user_id: str | None  # None = administrador via API_TOKEN
    email: str = ""
    is_admin: bool = False


ADMIN = Principal(user_id=None, email="admin", is_admin=True)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_b64, digest_b64 = stored.split("$")
        digest = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), **SCRYPT)
        return hmac.compare_digest(digest, base64.b64decode(digest_b64))
    except (ValueError, TypeError):
        return False


@lru_cache
def _secret() -> str:
    settings = get_settings()
    configured = settings.jwt_secret.get_secret_value()
    if configured:
        return configured
    path = settings.data_dir / "jwt_secret"
    if path.exists():
        return path.read_text().strip()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(48)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value)
    return value


def reset_caches() -> None:
    _secret.cache_clear()


def issue_token(user: User) -> str:
    now = datetime.now(UTC)
    claims = {"sub": user.id, "email": user.email, "adm": user.is_admin, "iat": now,
              "exp": now + timedelta(days=get_settings().jwt_days)}
    return jwt.encode(claims, _secret(), algorithm="HS256")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def create_user(email: str, password: str, is_admin: bool = False) -> User:
    email = normalize_email(email)
    if "@" not in email or len(email) > 200:
        raise ValueError("e-mail inválido")
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"a senha precisa de pelo menos {MIN_PASSWORD} caracteres")
    with session() as s:
        if s.query(User).filter(User.email == email).first():
            raise ValueError("e-mail já cadastrado")
        user = User(email=email, password_hash=hash_password(password), is_admin=is_admin)
        s.add(user)
        s.commit()
        return user


def authenticate(email: str, password: str) -> User | None:
    with session() as s:
        user = s.query(User).filter(User.email == normalize_email(email)).first()
    if user is None:
        hash_password(password)  # tempo parecido para e-mail inexistente
        return None
    return user if verify_password(password, user.password_hash) else None


def current_principal(authorization: Annotated[str | None, Header()] = None) -> Principal:
    settings = get_settings()
    admin_token = settings.api_token.get_secret_value()
    scheme, _, value = (authorization or "").partition(" ")
    value = value.strip()
    unauthorized = HTTPException(status.HTTP_401_UNAUTHORIZED, "token inválido ou expirado",
                                 headers={"WWW-Authenticate": "Bearer"})
    if scheme.lower() != "bearer" or not value:
        if not admin_token and not _has_users():
            return ADMIN  # instalação local sem nenhuma credencial configurada (desenvolvimento)
        raise unauthorized
    if admin_token and secrets.compare_digest(value, admin_token):
        return ADMIN
    try:
        claims = jwt.decode(value, _secret(), algorithms=["HS256"])
    except jwt.PyJWTError:
        raise unauthorized from None
    with session() as s:
        user = s.get(User, claims.get("sub"))
    if user is None:
        raise unauthorized
    return Principal(user_id=user.id, email=user.email, is_admin=user.is_admin)


def _has_users() -> bool:
    with session() as s:
        return s.query(User.id).first() is not None
