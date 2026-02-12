"""
Async MySQL database helper using aiomysql connection pooling.
"""
import aiomysql
import os
import json
import asyncio
import pathlib


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
        await cls._run_schema()

    @classmethod
    async def _run_schema(cls) -> None:
        """Auto-create tables from db/schema.sql if they don't exist."""
        schema_path = pathlib.Path(__file__).parent / "schema.sql"
        if not schema_path.exists():
            print("   schema.sql not found, skipping auto-migration.")
            return
        sql = schema_path.read_text(encoding="utf-8")
        # Split on semicolons, run each statement
        statements = [s.strip() for s in sql.split(";") if s.strip()]
        async with cls._pool.acquire() as conn:
            async with conn.cursor() as cur:
                for stmt in statements:
                    if not stmt:
                        continue
                    try:
                        await cur.execute(stmt)
                    except Exception as e:
                        print(f"   Schema statement warning: {e}")
        print("   Auto-migration complete (schema.sql applied).")

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
    async def insert_get_id(cls, query: str, args: tuple = ()) -> int:
        """Execute an INSERT query and return the new auto-increment ID."""
        async with cls._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, args)
                return cur.lastrowid


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
