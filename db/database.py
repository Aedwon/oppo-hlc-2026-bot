"""
Async MySQL database helper using aiomysql connection pooling.
"""
import aiomysql
import os
import json
import asyncio


class Database:
    """Manages an aiomysql connection pool with convenience helpers."""

    _pool: aiomysql.Pool | None = None

    @classmethod
    async def create_pool(cls) -> None:
        """Initialise the connection pool from environment variables."""
        if cls._pool is not None:
            return
        cls._pool = await aiomysql.create_pool(
            host=os.getenv("DB_HOST", "localhost"),
            port=int(os.getenv("DB_PORT", 3306)),
            user=os.getenv("DB_USER", "oppo_bot"),
            password=os.getenv("DB_PASSWORD", ""),
            db=os.getenv("DB_NAME", "oppo_hlc_bot"),
            charset="utf8mb4",
            autocommit=True,
            minsize=2,
            maxsize=10,
            pool_recycle=300,  # Reconnect idle connections every 5 min
        )

    @classmethod
    async def close(cls) -> None:
        """Close the pool gracefully."""
        if cls._pool:
            cls._pool.close()
            await cls._pool.wait_closed()
            cls._pool = None

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    @classmethod
    async def execute(cls, query: str, args: tuple = ()) -> int:
        """Execute a write query (INSERT / UPDATE / DELETE).
        Returns the number of affected rows.
        """
        async with cls._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, args)
                return cur.rowcount

    @classmethod
    async def fetchone(cls, query: str, args: tuple = ()) -> dict | None:
        """Fetch a single row as a dict."""
        async with cls._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query, args)
                return await cur.fetchone()

    @classmethod
    async def fetchall(cls, query: str, args: tuple = ()) -> list[dict]:
        """Fetch all rows as a list of dicts."""
        async with cls._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query, args)
                return await cur.fetchall()

    @classmethod
    async def fetchval(cls, query: str, args: tuple = ()):
        """Fetch the first column of the first row (scalar value)."""
        async with cls._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, args)
                row = await cur.fetchone()
                return row[0] if row else None

    @classmethod
    async def executemany(cls, query: str, args_list: list[tuple]) -> int:
        """Execute the same query with multiple arg sets (bulk insert)."""
        async with cls._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(query, args_list)
                return cur.rowcount

    # ------------------------------------------------------------------
    # Convenience: guild config
    # ------------------------------------------------------------------

    @classmethod
    async def get_config(cls, guild_id: int, key: str) -> str | None:
        row = await cls.fetchone(
            "SELECT config_value FROM guild_config WHERE guild_id = %s AND config_key = %s",
            (guild_id, key),
        )
        return row["config_value"] if row else None

    @classmethod
    async def set_config(cls, guild_id: int, key: str, value: str) -> None:
        await cls.execute(
            "INSERT INTO guild_config (guild_id, config_key, config_value) "
            "VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE config_value = VALUES(config_value)",
            (guild_id, key, value),
        )

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    @classmethod
    async def test_connection(cls) -> bool:
        """Quick connectivity check."""
        try:
            await cls.create_pool()
            val = await cls.fetchval("SELECT 1")
            print(f"✅ Database connection OK (returned {val})")
            return True
        except Exception as e:
            print(f"❌ Database connection FAILED: {e}")
            return False
        finally:
            await cls.close()
