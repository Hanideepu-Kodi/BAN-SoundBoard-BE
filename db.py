from contextlib import asynccontextmanager
import asyncpg
from config import settings


pool: asyncpg.Pool | None = None


async def init_pool() -> None:
    global pool
    pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=1,
        max_size=5,
    )


async def close_pool() -> None:
    if pool:
        await pool.close()


@asynccontextmanager
async def get_db(user_id: str | None, role: str = "authenticated"):
    if pool is None:
        raise RuntimeError("Database pool is not initialized.")
    conn = await pool.acquire()
    try:
        await conn.execute("set local row_security = on")
        if user_id:
            await conn.execute("select set_config('request.jwt.claim.sub', $1, true)", user_id)
            await conn.execute("select set_config('request.jwt.claim.role', $1, true)", role)
        else:
            await conn.execute("select set_config('request.jwt.claim.sub', $1, true)", "")
            await conn.execute("select set_config('request.jwt.claim.role', $1, true)", "anon")
        yield conn
    finally:
        await pool.release(conn)


@asynccontextmanager
async def get_service_db():
    if pool is None:
        raise RuntimeError("Database pool is not initialized.")
    conn = await pool.acquire()
    try:
        await conn.execute("set local row_security = off")
        yield conn
    finally:
        await pool.release(conn)
