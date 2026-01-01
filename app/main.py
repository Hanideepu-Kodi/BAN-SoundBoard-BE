import hashlib
import re
import secrets
import uuid
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from storage3.utils import StorageException

from app.auth import AuthUser, get_current_user, get_optional_user
from app.config import settings
from app.db import close_pool, get_db, get_service_db, init_pool
from app.schemas import AddSoundRequest, PlaylistCreate, PlaylistUpdate, ReorderRequest, SoundUpdate
from app.storage import create_signed_url, delete_path, safe_name, upload_bytes


app = FastAPI(title="Kodi-board API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup() -> None:
    await init_pool()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    await close_pool()


def normalize_privacy(value: str | None) -> str:
    if value in ("public", "private", "link_only"):
        return value
    if value == "link-only":
        return "link_only"
    return "link_only"


def normalize_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    tags = [tag.strip() for tag in raw.split(",")]
    return [tag for tag in tags if tag]


def slugify(tag: str) -> str:
    tag = tag.strip().lower()
    tag = re.sub(r"[^a-z0-9]+", "-", tag)
    return tag.strip("-")


async def ensure_tags(conn, tags: list[str]) -> list[str]:
    tag_ids: list[str] = []
    for tag in tags:
        slug = slugify(tag)
        if not slug:
            continue
        row = await conn.fetchrow(
            """
            INSERT INTO tags (slug, display)
            VALUES ($1, $2)
            ON CONFLICT (slug)
            DO UPDATE SET display = EXCLUDED.display
            RETURNING id
            """,
            slug,
            tag,
        )
        tag_ids.append(row["id"])
    return tag_ids


async def attach_tags(conn, sound_id: str, tags: list[str]) -> list[str]:
    tag_ids = await ensure_tags(conn, tags)
    if not tag_ids:
        return []
    for tag_id in tag_ids:
        await conn.execute(
            """
            INSERT INTO sound_tags (sound_id, tag_id)
            VALUES ($1, $2)
            ON CONFLICT DO NOTHING
            """,
            sound_id,
            tag_id,
        )
    return tags


async def fetch_profiles(profile_ids: list[str]) -> dict[str, dict]:
    if not profile_ids:
        return {}
    unique_ids = list({pid for pid in profile_ids if pid})
    async with get_service_db() as conn:
        rows = await conn.fetch(
            """
            SELECT id, handle, display_name, avatar_url
            FROM profiles
            WHERE id = ANY($1::uuid[])
            """,
            unique_ids,
        )
    return {
        str(row["id"]): {
            "id": str(row["id"]),
            "handle": row["handle"],
            "display_name": row["display_name"],
            "avatar_url": row["avatar_url"],
        }
        for row in rows
    }


def map_sound_row(row, signed_url: str, creator: dict | None = None) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "url": signed_url,
        "tags": row["tags"] or [],
        "privacy": row["privacy"],
        "duration_seconds": row["duration_seconds"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "owner_id": str(row["owner_id"]) if row["owner_id"] else None,
        "creator": creator,
    }


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/playlists")
async def list_playlists(current_user: AuthUser = Depends(get_current_user)) -> dict:
    async with get_db(current_user.id, current_user.role) as conn:
        rows = await conn.fetch(
            """
            SELECT p.id,
                   p.owner_id,
                   p.name,
                   p.description,
                   p.privacy,
                   p.created_at,
                   COUNT(ps.sound_id) as sound_count
            FROM playlists p
            LEFT JOIN playlist_sounds ps ON ps.playlist_id = p.id
            GROUP BY p.id
            ORDER BY p.created_at DESC
            """
        )
    playlists = [
        {
            "id": row["id"],
            "owner_id": str(row["owner_id"]),
            "name": row["name"],
            "description": row["description"],
            "privacy": row["privacy"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "sound_count": row["sound_count"],
        }
        for row in rows
    ]
    return {"playlists": playlists}


@app.post("/playlists")
async def create_playlist(
    payload: PlaylistCreate,
    current_user: AuthUser = Depends(get_current_user),
) -> dict:
    privacy = normalize_privacy(payload.privacy)
    share_token = secrets.token_urlsafe(16)
    share_token_hash = hashlib.sha256(share_token.encode("utf-8")).hexdigest()
    async with get_db(current_user.id, current_user.role) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO playlists (id, owner_id, name, description, privacy, share_token_hash)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id, name, description, privacy, created_at
            """,
            str(uuid.uuid4()),
            current_user.id,
            payload.name,
            payload.description,
            privacy,
            share_token_hash,
        )

    playlist = {
        "id": row["id"],
        "owner_id": str(current_user.id),
        "name": row["name"],
        "description": row["description"],
        "privacy": row["privacy"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "sound_count": 0,
        "share_token": share_token,
    }
    return {"playlist": playlist}


@app.post("/playlists/{playlist_id}/share")
async def rotate_share_token(
    playlist_id: str,
    current_user: AuthUser = Depends(get_current_user),
) -> dict:
    share_token = secrets.token_urlsafe(16)
    share_token_hash = hashlib.sha256(share_token.encode("utf-8")).hexdigest()
    async with get_db(current_user.id, current_user.role) as conn:
        row = await conn.fetchrow(
            """
            UPDATE playlists
            SET share_token_hash = $1
            WHERE id = $2
            RETURNING id
            """,
            share_token_hash,
            playlist_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Playlist not found.")
    return {"share_token": share_token}


@app.get("/playlists/{playlist_id}")
async def get_playlist(
    playlist_id: str,
    current_user: Optional[AuthUser] = Depends(get_optional_user),
) -> dict:
    async with get_db(
        current_user.id if current_user else None,
        current_user.role if current_user else "anon",
    ) as conn:
        playlist = await conn.fetchrow(
            """
            SELECT id, owner_id, name, description, privacy, created_at
            FROM playlists
            WHERE id = $1
            """,
            playlist_id,
        )
        if not playlist:
            raise HTTPException(status_code=404, detail="Playlist not found.")

        sounds = await conn.fetch(
            """
            SELECT s.id,
                   s.owner_id,
                   s.name,
                   s.storage_path,
                   s.privacy,
                   s.duration_seconds,
                   s.created_at,
                   COALESCE(array_agg(t.display) FILTER (WHERE t.id IS NOT NULL), '{}') as tags
            FROM playlist_sounds ps
            JOIN sounds s ON s.id = ps.sound_id
            LEFT JOIN sound_tags st ON st.sound_id = s.id
            LEFT JOIN tags t ON t.id = st.tag_id
            WHERE ps.playlist_id = $1
            GROUP BY s.id, ps.position
            ORDER BY ps.position ASC NULLS LAST, s.created_at DESC
            """,
            playlist_id,
        )

    profile_map = await fetch_profiles(
        [str(playlist["owner_id"])] + [str(row["owner_id"]) for row in sounds if row["owner_id"]]
    )
    mapped_sounds = []
    for row in sounds:
        signed_url = await create_signed_url(row["storage_path"])
        mapped_sounds.append(map_sound_row(row, signed_url, profile_map.get(str(row["owner_id"]))))

    return {
        "playlist": {
            "id": playlist["id"],
            "owner_id": str(playlist["owner_id"]),
            "name": playlist["name"],
            "description": playlist["description"],
            "privacy": playlist["privacy"],
            "created_at": playlist["created_at"].isoformat() if playlist["created_at"] else None,
            "sound_count": len(mapped_sounds),
            "creator": profile_map.get(str(playlist["owner_id"])),
        },
        "sounds": mapped_sounds,
    }


@app.patch("/playlists/{playlist_id}")
async def update_playlist(
    playlist_id: str,
    payload: PlaylistUpdate,
    current_user: AuthUser = Depends(get_current_user),
) -> dict:
    privacy_value = normalize_privacy(payload.privacy) if payload.privacy is not None else None
    async with get_db(current_user.id, current_user.role) as conn:
        row = await conn.fetchrow(
            """
            UPDATE playlists
            SET name = COALESCE($1, name),
                description = COALESCE($2, description),
                privacy = COALESCE($3, privacy)
            WHERE id = $4
            RETURNING id, owner_id, name, description, privacy, created_at
            """,
            payload.name,
            payload.description,
            privacy_value,
            playlist_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Playlist not found.")

    profile_map = await fetch_profiles([str(row["owner_id"])])
    return {
        "playlist": {
            "id": row["id"],
            "owner_id": str(row["owner_id"]),
            "name": row["name"],
            "description": row["description"],
            "privacy": row["privacy"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "sound_count": 0,
            "creator": profile_map.get(str(row["owner_id"])),
        }
    }


@app.get("/playlists/share/{token}")
async def get_playlist_share(token: str) -> dict:
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    async with get_service_db() as conn:
        playlist = await conn.fetchrow(
            """
            SELECT id, owner_id, name, description, privacy, created_at
            FROM playlists
            WHERE share_token_hash = $1
              AND privacy = 'link_only'
            """,
            token_hash,
        )
        if not playlist:
            raise HTTPException(status_code=404, detail="Share link not found.")

        sounds = await conn.fetch(
            """
            SELECT s.id,
                   s.owner_id,
                   s.name,
                   s.storage_path,
                   s.privacy,
                   s.duration_seconds,
                   s.created_at,
                   COALESCE(array_agg(t.display) FILTER (WHERE t.id IS NOT NULL), '{}') as tags
            FROM playlist_sounds ps
            JOIN sounds s ON s.id = ps.sound_id
            LEFT JOIN sound_tags st ON st.sound_id = s.id
            LEFT JOIN tags t ON t.id = st.tag_id
            WHERE ps.playlist_id = $1
            GROUP BY s.id, ps.position
            ORDER BY ps.position ASC NULLS LAST, s.created_at DESC
            """,
            playlist["id"],
        )

    profile_map = await fetch_profiles(
        [str(playlist["owner_id"])] + [str(row["owner_id"]) for row in sounds if row["owner_id"]]
    )
    mapped_sounds = []
    for row in sounds:
        signed_url = await create_signed_url(row["storage_path"])
        mapped_sounds.append(map_sound_row(row, signed_url, profile_map.get(str(row["owner_id"]))))

    return {
        "playlist": {
            "id": playlist["id"],
            "owner_id": str(playlist["owner_id"]),
            "name": playlist["name"],
            "description": playlist["description"],
            "privacy": playlist["privacy"],
            "created_at": playlist["created_at"].isoformat() if playlist["created_at"] else None,
            "sound_count": len(mapped_sounds),
            "creator": profile_map.get(str(playlist["owner_id"])),
        },
        "sounds": mapped_sounds,
    }


@app.post("/playlists/{playlist_id}/sounds")
async def add_sound_to_playlist(
    playlist_id: str,
    payload: AddSoundRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> dict:
    async with get_db(current_user.id, current_user.role) as conn:
        playlist = await conn.fetchrow(
            "SELECT id FROM playlists WHERE id = $1",
            playlist_id,
        )
        if not playlist:
            raise HTTPException(status_code=404, detail="Playlist not found.")

        sound = await conn.fetchrow(
            "SELECT id FROM sounds WHERE id = $1",
            payload.sound_id,
        )
        if not sound:
            raise HTTPException(status_code=404, detail="Sound not found.")

        position_row = await conn.fetchrow(
            "SELECT COALESCE(MAX(position), 0) + 1 as position FROM playlist_sounds WHERE playlist_id = $1",
            playlist_id,
        )
        await conn.execute(
            """
            INSERT INTO playlist_sounds (playlist_id, sound_id, position)
            VALUES ($1, $2, $3)
            ON CONFLICT DO NOTHING
            """,
            playlist_id,
            payload.sound_id,
            position_row["position"],
        )

    return {"ok": True}


@app.delete("/playlists/{playlist_id}/sounds")
async def remove_sound_from_playlist(
    playlist_id: str,
    payload: AddSoundRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> dict:
    async with get_db(current_user.id, current_user.role) as conn:
        await conn.execute(
            "DELETE FROM playlist_sounds WHERE playlist_id = $1 AND sound_id = $2",
            playlist_id,
            payload.sound_id,
        )
    return {"ok": True}


@app.put("/playlists/{playlist_id}/sounds")
async def reorder_playlist_sounds(
    playlist_id: str,
    payload: ReorderRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> dict:
    async with get_db(current_user.id, current_user.role) as conn:
        for index, sound_id in enumerate(payload.sound_ids, start=1):
            await conn.execute(
                """
                UPDATE playlist_sounds
                SET position = $1
                WHERE playlist_id = $2 AND sound_id = $3
                """,
                index,
                playlist_id,
                sound_id,
            )
    return {"ok": True}


@app.get("/sounds")
async def list_sounds(
    q: Optional[str] = None,
    tag: Optional[str] = None,
    owner_id: Optional[str] = None,
    current_user: Optional[AuthUser] = Depends(get_optional_user),
) -> dict:
    query = f"%{q.strip()}%" if q else None
    async with get_db(
        current_user.id if current_user else None,
        current_user.role if current_user else "anon",
    ) as conn:
        rows = await conn.fetch(
            """
            SELECT s.id,
                   s.owner_id,
                   s.name,
                   s.storage_path,
                   s.privacy,
                   s.duration_seconds,
                   s.created_at,
                   COALESCE(array_agg(t.display) FILTER (WHERE t.id IS NOT NULL), '{}') as tags
            FROM sounds s
            LEFT JOIN sound_tags st ON st.sound_id = s.id
            LEFT JOIN tags t ON t.id = st.tag_id
            WHERE ($1::text IS NULL OR s.name ILIKE $1 OR t.display ILIKE $1)
              AND ($2::text IS NULL OR t.slug = $2)
              AND ($3::uuid IS NULL OR s.owner_id = $3)
            GROUP BY s.id
            ORDER BY s.created_at DESC
            """,
            query,
            tag,
            owner_id,
        )

    profile_map = await fetch_profiles([str(row["owner_id"]) for row in rows if row["owner_id"]])
    sounds = []
    for row in rows:
        signed_url = await create_signed_url(row["storage_path"])
        sounds.append(map_sound_row(row, signed_url, profile_map.get(str(row["owner_id"]))))
    return {"sounds": sounds}


@app.get("/creators/{creator_id}")
async def get_creator(
    creator_id: str,
    current_user: Optional[AuthUser] = Depends(get_optional_user),
) -> dict:
    async with get_service_db() as conn:
        profile = await conn.fetchrow(
            """
            SELECT id, handle, display_name, avatar_url
            FROM profiles
            WHERE id = $1
            """,
            creator_id,
        )
    if profile:
        profile_data = {
            "id": str(profile["id"]),
            "handle": profile["handle"],
            "display_name": profile["display_name"],
            "avatar_url": profile["avatar_url"],
        }
    else:
        profile_data = {
            "id": creator_id,
            "handle": None,
            "display_name": None,
            "avatar_url": None,
        }

    async with get_db(
        current_user.id if current_user else None,
        current_user.role if current_user else "anon",
    ) as conn:
        sound_rows = await conn.fetch(
            """
            SELECT s.id,
                   s.owner_id,
                   s.name,
                   s.storage_path,
                   s.privacy,
                   s.duration_seconds,
                   s.created_at,
                   COALESCE(array_agg(t.display) FILTER (WHERE t.id IS NOT NULL), '{}') as tags
            FROM sounds s
            LEFT JOIN sound_tags st ON st.sound_id = s.id
            LEFT JOIN tags t ON t.id = st.tag_id
            WHERE s.owner_id = $1
            GROUP BY s.id
            ORDER BY s.created_at DESC
            """,
            creator_id,
        )
        playlist_rows = await conn.fetch(
            """
            SELECT p.id,
                   p.owner_id,
                   p.name,
                   p.description,
                   p.privacy,
                   p.created_at,
                   COUNT(ps.sound_id) as sound_count
            FROM playlists p
            LEFT JOIN playlist_sounds ps ON ps.playlist_id = p.id
            WHERE p.owner_id = $1
            GROUP BY p.id
            ORDER BY p.created_at DESC
            """,
            creator_id,
        )

    sounds = []
    for row in sound_rows:
        signed_url = await create_signed_url(row["storage_path"])
        sounds.append(map_sound_row(row, signed_url, profile_data))

    playlists = [
        {
            "id": row["id"],
            "owner_id": str(row["owner_id"]),
            "name": row["name"],
            "description": row["description"],
            "privacy": row["privacy"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "sound_count": row["sound_count"],
            "creator": profile_data,
        }
        for row in playlist_rows
    ]

    return {
        "profile": profile_data,
        "stats": {
            "plays": 0,
            "sounds": len(sounds),
            "playlists": len(playlists),
        },
        "sounds": sounds,
        "playlists": playlists,
    }


@app.post("/sounds")
async def create_sound(
    file: UploadFile = File(...),
    name: str = Form(""),
    description: str = Form(""),
    tags: str = Form(""),
    privacy: str = Form("link_only"),
    current_user: AuthUser = Depends(get_current_user),
) -> dict:
    display_name = name.strip() if name.strip() else file.filename
    if not display_name:
        raise HTTPException(status_code=400, detail="Sound name required.")

    sound_id = str(uuid.uuid4())
    filename = safe_name(file.filename or display_name)
    storage_path = f"{current_user.id}/{sound_id}/{filename}"
    data = await file.read()
    try:
        await upload_bytes(storage_path, data, file.content_type)
    except StorageException as exc:
        detail = "Storage upload failed."
        if exc.args and isinstance(exc.args[0], dict):
            detail = exc.args[0].get("message") or detail
        raise HTTPException(status_code=502, detail=detail) from exc

    privacy_value = normalize_privacy(privacy)
    tag_list = normalize_tags(tags)

    async with get_db(current_user.id, current_user.role) as conn:
        await conn.execute(
            """
            INSERT INTO sounds (id, owner_id, name, description, storage_path, size_bytes, format, privacy)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            sound_id,
            current_user.id,
            display_name,
            description or None,
            storage_path,
            len(data),
            file.content_type,
            privacy_value,
        )
        await attach_tags(conn, sound_id, tag_list)

    signed_url = await create_signed_url(storage_path)
    creator = (await fetch_profiles([str(current_user.id)])).get(str(current_user.id))
    return {
        "sound": {
            "id": sound_id,
            "name": display_name,
            "url": signed_url,
            "tags": tag_list,
            "privacy": privacy_value,
            "duration_seconds": None,
            "created_at": None,
            "owner_id": str(current_user.id),
            "creator": creator,
        }
    }


@app.patch("/sounds/{sound_id}")
async def update_sound(
    sound_id: str,
    payload: SoundUpdate,
    current_user: AuthUser = Depends(get_current_user),
) -> dict:
    privacy_value = normalize_privacy(payload.privacy) if payload.privacy is not None else None
    tags_list = normalize_tags(payload.tags) if payload.tags is not None else None
    async with get_db(current_user.id, current_user.role) as conn:
        row = await conn.fetchrow("SELECT id FROM sounds WHERE id = $1", sound_id)
        if not row:
            raise HTTPException(status_code=404, detail="Sound not found.")
        await conn.execute(
            """
            UPDATE sounds
            SET name = COALESCE($1, name),
                privacy = COALESCE($2, privacy)
            WHERE id = $3
            """,
            payload.name,
            privacy_value,
            sound_id,
        )
        if tags_list is not None:
            await conn.execute("DELETE FROM sound_tags WHERE sound_id = $1", sound_id)
            await attach_tags(conn, sound_id, tags_list)

    return {"ok": True}


@app.delete("/sounds/{sound_id}")
async def delete_sound(
    sound_id: str,
    current_user: AuthUser = Depends(get_current_user),
) -> dict:
    async with get_db(current_user.id, current_user.role) as conn:
        row = await conn.fetchrow("SELECT storage_path FROM sounds WHERE id = $1", sound_id)
        if not row:
            raise HTTPException(status_code=404, detail="Sound not found.")
        await conn.execute("DELETE FROM sounds WHERE id = $1", sound_id)
    await delete_path(row["storage_path"])
    return {"ok": True}
