from typing import Optional
import re
import jwt
from jwt import PyJWKClient
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from config import settings
from db import get_service_db


security = HTTPBearer(auto_error=False)
_jwks_client: PyJWKClient | None = None


class AuthUser:
    def __init__(self, user_id: str, role: str = "authenticated"):
        self.id = user_id
        self.role = role


def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        jwks_url = f"{settings.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
        _jwks_client = PyJWKClient(jwks_url)
    return _jwks_client


def decode_token(token: str) -> dict:
    try:
        header = jwt.get_unverified_header(token)
        alg = header.get("alg")

        if alg and alg.startswith("HS"):
            return jwt.decode(
                token,
                settings.supabase_jwt_secret,
                algorithms=[alg],
                options={"verify_aud": False},
            )

        signing_key = _get_jwks_client().get_signing_key_from_jwt(token).key
        return jwt.decode(
            token,
            signing_key,
            algorithms=[alg] if alg else ["RS256", "ES256"],
            options={"verify_aud": False},
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired.") from exc
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token.") from exc


def _slugify_handle(value: str) -> str:
    handle = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    return handle.strip("-") or "creator"


async def ensure_profile(user_id: str, payload: dict) -> None:
    metadata = payload.get("user_metadata") or {}
    email = payload.get("email") or metadata.get("email")
    display_name = (
        metadata.get("full_name")
        or metadata.get("name")
        or metadata.get("preferred_username")
        or metadata.get("user_name")
    )
    if not display_name and email:
        display_name = email.split("@")[0]
    avatar_url = metadata.get("avatar_url") or metadata.get("picture")
    base_handle = _slugify_handle(display_name or (email.split("@")[0] if email else "creator"))
    handle = f"{base_handle}-{user_id[:6]}"

    async with get_service_db() as conn:
        await conn.execute(
            """
            INSERT INTO profiles (id, handle, display_name, avatar_url)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (id) DO UPDATE
            SET handle = COALESCE(profiles.handle, EXCLUDED.handle),
                display_name = COALESCE(NULLIF(profiles.display_name, ''), EXCLUDED.display_name),
                avatar_url = COALESCE(NULLIF(profiles.avatar_url, ''), EXCLUDED.avatar_url)
            """,
            user_id,
            handle,
            display_name,
            avatar_url,
        )


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> AuthUser:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token.")
    payload = decode_token(credentials.credentials)
    user_id = payload.get("sub")
    role = payload.get("role", "authenticated")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token.")
    await ensure_profile(user_id, payload)
    return AuthUser(user_id=user_id, role=role)


async def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Optional[AuthUser]:
    if credentials is None:
        return None
    payload = decode_token(credentials.credentials)
    user_id = payload.get("sub")
    role = payload.get("role", "authenticated")
    if not user_id:
        return None
    await ensure_profile(user_id, payload)
    return AuthUser(user_id=user_id, role=role)
