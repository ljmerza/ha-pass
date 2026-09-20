"""SQLite database setup and CRUD operations.

Note: Uses a single aiosqlite connection for all operations. This serializes
all DB access (reads block writes and vice versa), which is acceptable at
homelab scale with low concurrent users. For higher concurrency, consider
connection pooling or switching to PostgreSQL.
"""
import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import aiosqlite

from app.config import settings
logger = logging.getLogger(__name__)

_db: aiosqlite.Connection | None = None
_lock = asyncio.Lock()


def run_migrations() -> None:
    """Run Alembic migrations synchronously (called before the async event loop)."""
    from alembic.config import Config
    from alembic import command

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{settings.db_path}")
    command.upgrade(cfg, "head")


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        async with _lock:
            if _db is None:
                _db = await aiosqlite.connect(settings.db_path)
                _db.row_factory = aiosqlite.Row
                await _db.execute("PRAGMA journal_mode=WAL")
                await _db.execute("PRAGMA foreign_keys=ON")
    return _db


async def close_db() -> None:
    global _db
    if _db is not None:
        try:
            await _db.close()
        except Exception as exc:
            logger.warning("Error closing database: %s", exc)
        _db = None


# ---------------------------------------------------------------------------
# Admin sessions
# ---------------------------------------------------------------------------

async def create_admin_session(ttl_seconds: int) -> str:
    db = await get_db()
    session_id = uuid.uuid4().hex + uuid.uuid4().hex  # 64-char hex
    now = int(time.time())
    await db.execute(
        "INSERT INTO admin_sessions (id, created_at, expires_at) VALUES (?, ?, ?)",
        (session_id, now, now + ttl_seconds),
    )
    await db.commit()
    return session_id


async def get_admin_session(session_id: str) -> aiosqlite.Row | None:
    db = await get_db()
    async with db.execute(
        "SELECT * FROM admin_sessions WHERE id = ? AND expires_at > ?",
        (session_id, int(time.time())),
    ) as cur:
        return await cur.fetchone()


async def delete_admin_session(session_id: str) -> None:
    db = await get_db()
    await db.execute("DELETE FROM admin_sessions WHERE id = ?", (session_id,))
    await db.commit()


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

async def create_token(
    label: str,
    slug: str,
    entity_ids: list[str],
    expires_at: int,
    ip_allowlist: list[str] | None,
    entity_meta: dict[str, dict[str, Any]] | None = None,
    pin_hash: str | None = None,
) -> dict[str, Any]:
    db = await get_db()
    token_id = str(uuid.uuid4())
    now = int(time.time())
    ip_json = json.dumps(ip_allowlist) if ip_allowlist else None

    # Deduplicate entity IDs
    entity_ids = list(dict.fromkeys(entity_ids))

    try:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            """INSERT INTO tokens
               (id, slug, label, created_at, expires_at, ip_allowlist, pin_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (token_id, slug, label, now, expires_at, ip_json, pin_hash),
        )
        if entity_ids:
            meta = entity_meta or {}
            await db.executemany(
                "INSERT INTO token_entities (token_id, entity_id, display_name, options) "
                "VALUES (?, ?, ?, ?)",
                [
                    (
                        token_id,
                        eid,
                        (meta.get(eid) or {}).get("display_name"),
                        json.dumps((meta.get(eid) or {}).get("options"))
                        if (meta.get(eid) or {}).get("options") else None,
                    )
                    for eid in entity_ids
                ],
            )
        await db.execute("COMMIT")
    except Exception:
        await db.execute("ROLLBACK")
        raise
    return await get_token_by_id(token_id)  # type: ignore[return-value]


async def get_token_by_slug(slug: str) -> aiosqlite.Row | None:
    db = await get_db()
    async with db.execute("SELECT * FROM tokens WHERE slug = ?", (slug,)) as cur:
        return await cur.fetchone()


async def get_token_by_id(token_id: str) -> aiosqlite.Row | None:
    db = await get_db()
    async with db.execute("SELECT * FROM tokens WHERE id = ?", (token_id,)) as cur:
        return await cur.fetchone()


async def list_tokens() -> list[aiosqlite.Row]:
    db = await get_db()
    async with db.execute(
        """SELECT t.*, COUNT(te.entity_id) AS entity_count
           FROM tokens t
           LEFT JOIN token_entities te ON te.token_id = t.id
           GROUP BY t.id
           ORDER BY t.created_at DESC"""
    ) as cur:
        return await cur.fetchall()


async def get_token_entities(token_id: str) -> list[str]:
    db = await get_db()
    async with db.execute(
        "SELECT entity_id FROM token_entities WHERE token_id = ?", (token_id,)
    ) as cur:
        rows = await cur.fetchall()
    return [r["entity_id"] for r in rows]


async def get_token_entity_meta(token_id: str) -> dict[str, dict[str, Any]]:
    """entity_id -> {"display_name": str|None, "options": dict}.

    Kept separate from get_token_entities() on purpose: that function returns the
    plain id list the allowlist checks depend on, and must not grow a shape the
    security path has to unpack.
    """
    db = await get_db()
    async with db.execute(
        "SELECT entity_id, display_name, options FROM token_entities WHERE token_id = ?",
        (token_id,),
    ) as cur:
        rows = await cur.fetchall()

    meta: dict[str, dict[str, Any]] = {}
    for r in rows:
        opts = {}
        if r["options"]:
            try:
                opts = json.loads(r["options"])
            except (ValueError, TypeError):
                opts = {}
        meta[r["entity_id"]] = {"display_name": r["display_name"], "options": opts}
    return meta


async def set_entity_meta(
    token_id: str,
    entity_id: str,
    display_name: str | None,
    options: dict[str, Any] | None,
) -> bool:
    """Set one entity's display name and options. False if not on the token."""
    db = await get_db()
    cur = await db.execute(
        "UPDATE token_entities SET display_name = ?, options = ? "
        "WHERE token_id = ? AND entity_id = ?",
        (display_name, json.dumps(options) if options else None, token_id, entity_id),
    )
    await db.commit()
    return cur.rowcount > 0


async def update_token_entities(
    token_id: str,
    entity_ids: list[str],
    entity_meta: dict[str, dict[str, Any]] | None = None,
) -> None:
    db = await get_db()
    # Deduplicate entity IDs
    entity_ids = list(dict.fromkeys(entity_ids))
    try:
        await db.execute("BEGIN IMMEDIATE")
        # This rebuilds the whole row set, so per-entity display names and options
        # already stored would be silently dropped unless they are read back first
        # and re-applied. An explicit entity_meta argument wins over what is stored.
        async with db.execute(
            "SELECT entity_id, display_name, options FROM token_entities WHERE token_id = ?",
            (token_id,),
        ) as cur:
            existing = {
                r["entity_id"]: (r["display_name"], r["options"])
                for r in await cur.fetchall()
            }
        for eid, m in (entity_meta or {}).items():
            name = m.get("display_name")
            opts = m.get("options")
            existing[eid] = (name, json.dumps(opts) if opts else None)

        await db.execute("DELETE FROM token_entities WHERE token_id = ?", (token_id,))
        await db.executemany(
            "INSERT INTO token_entities (token_id, entity_id, display_name, options) "
            "VALUES (?, ?, ?, ?)",
            [(token_id, eid, *existing.get(eid, (None, None))) for eid in entity_ids],
        )
        await db.execute("COMMIT")
    except Exception:
        await db.execute("ROLLBACK")
        raise


async def set_token_pin(token_id: str, pin_hash: str | None) -> None:
    """Set, replace, or (with None) clear a token's PIN.

    Guest PIN sessions are signed with a key derived from this column, so a write
    here is also the revocation mechanism — outstanding sessions stop verifying
    with no session rows to delete.
    """
    db = await get_db()
    await db.execute("UPDATE tokens SET pin_hash = ? WHERE id = ?", (pin_hash, token_id))
    await db.commit()


async def update_token_expiry(token_id: str, expires_at: int) -> None:
    db = await get_db()
    await db.execute(
        "UPDATE tokens SET expires_at = ? WHERE id = ?",
        (expires_at, token_id),
    )
    await db.commit()


async def revoke_token(token_id: str) -> None:
    db = await get_db()
    await db.execute("UPDATE tokens SET revoked = 1 WHERE id = ?", (token_id,))
    await db.commit()


async def unrevoke_token(token_id: str) -> None:
    db = await get_db()
    await db.execute("UPDATE tokens SET revoked = 0 WHERE id = ?", (token_id,))
    await db.commit()


async def delete_token(token_id: str) -> None:
    db = await get_db()
    # Nullify access_log references before deleting to avoid FK constraint
    # failures on databases where the ON DELETE SET NULL clause is missing.
    await db.execute("UPDATE access_log SET token_id = NULL WHERE token_id = ?", (token_id,))
    await db.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
    await db.commit()


async def touch_token(token_id: str) -> None:
    db = await get_db()
    await db.execute(
        "UPDATE tokens SET last_accessed = ? WHERE id = ?",
        (int(time.time()), token_id),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Access log
# ---------------------------------------------------------------------------

async def log_access(
    token_id: str,
    event_type: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
    entity_id: str | None = None,
    service: str | None = None,
) -> None:
    db = await get_db()
    await db.execute(
        """INSERT INTO access_log
           (token_id, timestamp, event_type, entity_id, service, ip_address, user_agent)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (token_id, int(time.time()), event_type, entity_id, service, ip_address, user_agent),
    )
    # Single-write commit is acceptable at homelab scale; batch for high throughput
    await db.commit()


async def list_access_logs(limit: int = 50) -> list[aiosqlite.Row]:
    db = await get_db()
    async with db.execute(
        """SELECT al.timestamp, al.event_type, al.entity_id, al.service,
                  al.ip_address, t.label AS token_label
           FROM access_log al
           LEFT JOIN tokens t ON t.id = al.token_id
           ORDER BY al.timestamp DESC, al.id DESC
           LIMIT ?""",
        (limit,),
    ) as cur:
        return await cur.fetchall()


async def cleanup_old_data(retention_days: int) -> None:
    """Delete old access_log rows and expired admin sessions.

    Guest tokens are intentionally retained until an admin deletes them so
    expired or revoked links can be renewed with the same entities and slug.
    """
    db = await get_db()
    now = int(time.time())
    cutoff = now - (retention_days * 86400)
    await db.execute("DELETE FROM access_log WHERE timestamp < ?", (cutoff,))
    await db.execute("DELETE FROM admin_sessions WHERE expires_at < ?", (now,))
    await db.commit()
