import asyncio
import re
from supabase import create_client, Client
from app.config import settings


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]", "-", value.strip())
    return cleaned or "sound"


supabase: Client = create_client(settings.supabase_url, settings.supabase_service_role_key)


async def upload_bytes(path: str, data: bytes, content_type: str | None) -> None:
    def _upload() -> None:
        supabase.storage.from_(settings.supabase_storage_bucket).upload(
            path,
            data,
            {"content-type": content_type or "application/octet-stream"},
        )

    await asyncio.to_thread(_upload)


async def create_signed_url(path: str) -> str:
    def _signed() -> str:
        response = supabase.storage.from_(settings.supabase_storage_bucket).create_signed_url(
            path,
            settings.signed_url_ttl_seconds,
        )
        return response.get("signedURL")

    return await asyncio.to_thread(_signed)


async def delete_path(path: str) -> None:
    def _delete() -> None:
        supabase.storage.from_(settings.supabase_storage_bucket).remove([path])

    await asyncio.to_thread(_delete)
