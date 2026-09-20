"""Admin API router."""
import asyncio
import ipaddress
import json
import secrets
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from app import database as db
from app.auth import INGRESS_SENTINEL, SESSION_COOKIE, require_admin, verify_password
from app.config import settings
from app import guest_pin
from app import ha_client
from app.models import (
    AdminLoginRequest,
    DISPLAY_NAME_MAX,
    ENTITY_OPTION_KEYS,
    EntityMetaRequest,
    NEVER_EXPIRES_SECONDS,
    SUPPORTED_DOMAINS,
    TokenCreateRequest,
    TokenPinRequest,
    TokenUpdateEntitiesRequest,
    TokenUpdateExpiryRequest,
)
from app.rate_limiter import RateLimiter

router = APIRouter(prefix="/admin")

# Admin session lifetime — 24 hours, hardcoded like Uptime Kuma / Dockge.
ADMIN_SESSION_TTL = 86400

# CSRF: Admin routes are protected by SameSite=strict cookie. The slug-based
# guest auth acts as a bearer token — no additional CSRF token needed.

# M-24: Rate limiting on admin login (5 failed attempts/min/IP)
_login_limiter = RateLimiter()

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@router.post("/login")
async def login(body: AdminLoginRequest, request: Request, response: Response) -> dict:
    if not settings.admin_password:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Login disabled — use HA sidebar")

    # Rate limit login attempts by IP
    client_ip = (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )
    allowed = await _login_limiter.check(f"login:{client_ip}", 5)
    if not allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many login attempts")

    if body.username != settings.admin_username or not await verify_password(body.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    is_https = (
        request.url.scheme == "https"
        or forwarded_proto == "https"
    )
    session_id = await db.create_admin_session(ttl_seconds=ADMIN_SESSION_TTL)
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        httponly=True,
        samesite="strict",
        secure=is_https,
        max_age=ADMIN_SESSION_TTL,
    )
    return {"ok": True}


@router.post("/logout")
async def logout(response: Response, session_id: str = Depends(require_admin)) -> dict:
    if session_id == INGRESS_SENTINEL:
        return {"ok": True}
    await db.delete_admin_session(session_id)
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

def _row_to_response(row: Any, entity_ids: list[str] | None = None,
                     entity_meta: dict[str, dict[str, Any]] | None = None) -> dict:
    ip_raw = row["ip_allowlist"]
    ip_list = json.loads(ip_raw) if ip_raw else None
    if entity_ids is not None:
        count = len(entity_ids)
    elif "entity_count" in row.keys():
        count = row["entity_count"]
    else:
        count = 0
    return {
        "id": row["id"],
        "slug": row["slug"],
        "label": row["label"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "revoked": bool(row["revoked"]),
        "last_accessed": row["last_accessed"],
        "ip_allowlist": ip_list,
        "entity_count": count,
        "entity_ids": entity_ids,
        "entity_meta": entity_meta,
        # Whether, never what — the PIN is stored as a bcrypt hash and there is
        # no path that returns it or the hash to the dashboard.
        "has_pin": bool(row["pin_hash"]),
    }


def _activity_row_to_response(row: Any) -> dict:
    return {
        "timestamp": row["timestamp"],
        "activity": row["event_type"],
        "token_label": row["token_label"],
        "target_entity_id": row["entity_id"],
        "service": row["service"],
        "ip_address": row["ip_address"],
    }


@router.get("/tokens")
async def list_tokens(_: str = Depends(require_admin)) -> list[dict]:
    rows = await db.list_tokens()
    return [_row_to_response(r) for r in rows]


@router.get("/activity")
async def list_activity(
    limit: int = Query(default=50, ge=1, le=200),
    _: str = Depends(require_admin),
) -> list[dict]:
    rows = await db.list_access_logs(limit=limit)
    return [_activity_row_to_response(r) for r in rows]


@router.post("/tokens", status_code=status.HTTP_201_CREATED)
async def create_token(
    body: TokenCreateRequest,
    request: Request,
    _: str = Depends(require_admin),
) -> dict:
    # Validate IP CIDR list if provided
    if body.ip_allowlist:
        for cidr in body.ip_allowlist:
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"Invalid CIDR: {cidr}",
                )

    slug = body.slug or secrets.token_hex(16)
    if body.expires_in_seconds == NEVER_EXPIRES_SECONDS:
        expires_at = NEVER_EXPIRES_SECONDS
    else:
        expires_at = int(time.time()) + body.expires_in_seconds

    # Ensure slug uniqueness
    existing = await db.get_token_by_slug(slug)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Slug '{slug}' already exists",
        )

    row = await db.create_token(
        label=body.label,
        slug=slug,
        entity_ids=body.entity_ids,
        expires_at=expires_at,
        ip_allowlist=body.ip_allowlist,
        entity_meta=_clean_entity_meta(body.entity_meta),
        pin_hash=await _hash_pin_or_none(body.pin),
    )
    entity_ids = await db.get_token_entities(row["id"])
    return _row_to_response(row, entity_ids)


async def _hash_pin_or_none(value: Any) -> str | None:
    """Validate a submitted PIN and hash it. Blank or None means 'no PIN'.

    The rejection message describes the policy without echoing the value — the
    PIN must not turn up in a response body, and an admin API error is a
    response body like any other.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str) or not guest_pin.is_valid_pin(value):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"PIN must be {guest_pin.PIN_MIN_LENGTH}-{guest_pin.PIN_MAX_LENGTH} digits"
            ),
        )
    return await guest_pin.hash_pin(value)


def _clean_name(value: Any) -> str | None:
    """Trim and cap a display name. Blank means 'no override'."""
    if not isinstance(value, str):
        return None
    return value.strip()[:DISPLAY_NAME_MAX] or None


def _clean_options(value: Any) -> dict[str, Any] | None:
    """Keep only allow-listed option keys, coerced to bool."""
    if not isinstance(value, dict):
        return None
    cleaned = {k: bool(v) for k, v in value.items() if k in ENTITY_OPTION_KEYS and v}
    return cleaned or None


def _clean_entity_meta(
    meta: dict[str, dict[str, Any]] | None,
) -> dict[str, dict[str, Any]] | None:
    """Normalise the per-entity override blob the dashboard posts.

    require_proximity is read from the top level of each entry, never from
    `options` — it is stored in its own column because the command path enforces
    it, and letting it arrive inside the presentation blob would blur exactly
    the line that column exists to keep.
    """
    if not meta:
        return None
    cleaned = {}
    for eid, m in meta.items():
        if not isinstance(m, dict):
            continue
        name = _clean_name(m.get("display_name"))
        opts = _clean_options(m.get("options"))
        gated = bool(m.get("require_proximity"))
        if name or opts or gated:
            cleaned[eid] = {
                "display_name": name,
                "options": opts,
                "require_proximity": gated,
            }
    return cleaned or None


@router.patch("/tokens/{token_id}/entity-meta")
async def set_entity_meta(
    token_id: str,
    body: EntityMetaRequest,
    _: str = Depends(require_admin),
) -> dict:
    row = await db.get_token_by_id(token_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    name = _clean_name(body.display_name)
    opts = _clean_options(body.options)

    updated = await db.set_entity_meta(
        token_id, body.entity_id, name, opts, body.require_proximity
    )
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Entity not on this token",
        )

    await ha_client.invalidate_entity_cache(token_id)
    return {
        "entity_id": body.entity_id,
        "display_name": name,
        "options": opts or {},
        "require_proximity": body.require_proximity,
    }


@router.get("/tokens/{token_id}")
async def get_token(token_id: str, _: str = Depends(require_admin)) -> dict:
    row = await db.get_token_by_id(token_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    entity_ids = await db.get_token_entities(token_id)
    meta = await db.get_token_entity_meta(token_id)
    return _row_to_response(row, entity_ids, meta)


@router.patch("/tokens/{token_id}/entities")
async def update_token_entities(
    token_id: str,
    body: TokenUpdateEntitiesRequest,
    _: str = Depends(require_admin),
) -> dict:
    row = await db.get_token_by_id(token_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if row["revoked"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot edit entities on a revoked token",
        )
    await db.update_token_entities(
        token_id, body.entity_ids, _clean_entity_meta(body.entity_meta)
    )
    await ha_client.invalidate_entity_cache(token_id)
    entity_ids = await db.get_token_entities(token_id)
    meta = await db.get_token_entity_meta(token_id)
    row = await db.get_token_by_id(token_id)
    return _row_to_response(row, entity_ids, meta)


@router.patch("/tokens/{token_id}/expiry")
async def update_token_expiry(
    token_id: str,
    body: TokenUpdateExpiryRequest,
    _: str = Depends(require_admin),
) -> dict:
    row = await db.get_token_by_id(token_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if body.expires_in_seconds == NEVER_EXPIRES_SECONDS:
        new_expires = NEVER_EXPIRES_SECONDS
    else:
        new_expires = int(time.time()) + body.expires_in_seconds
    await db.update_token_expiry(token_id, new_expires)
    # Un-revoke if the token was revoked (admin is explicitly renewing it)
    if row["revoked"]:
        await db.unrevoke_token(token_id)
    row = await db.get_token_by_id(token_id)
    return _row_to_response(row)


@router.patch("/tokens/{token_id}/pin")
async def update_token_pin(
    token_id: str,
    body: TokenPinRequest,
    _: str = Depends(require_admin),
) -> dict:
    """Set, replace, or clear the token's PIN.

    There is no read side. Changing or clearing the PIN also invalidates every
    guest PIN session for the token, because those cookies are signed with a key
    derived from the hash this writes.
    """
    row = await db.get_token_by_id(token_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    pin_hash = await _hash_pin_or_none(body.pin)
    await db.set_token_pin(token_id, pin_hash)
    return {"has_pin": pin_hash is not None}


@router.post("/tokens/{token_id}/revoke")
async def revoke_token(token_id: str, _: str = Depends(require_admin)) -> dict:
    row = await db.get_token_by_id(token_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    await db.revoke_token(token_id)
    # Notify connected SSE clients
    if not row["revoked"]:
        await ha_client.broadcast_token_expired(token_id)
    return {"ok": True}


@router.delete("/tokens/{token_id}")
async def delete_token(token_id: str, _: str = Depends(require_admin)) -> dict:
    row = await db.get_token_by_id(token_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    await ha_client.broadcast_token_expired(token_id)
    await db.delete_token(token_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# HA entity list proxy
# ---------------------------------------------------------------------------

@router.get("/ha/entities")
async def ha_entities(
    include_labels: bool = False,
    _: str = Depends(require_admin),
) -> Any:
    """List the entities guests may be given, optionally with HA labels.

    Without include_labels the response is the bare entity list it has always
    been, and no registry read happens. With it, the response becomes an
    envelope carrying the same list (each entity gaining `labels`) plus the
    label catalogue, so the picker gets both halves of a label filter in one
    round trip. Labels come from HA's registries over the WebSocket API; when
    those cannot be read the envelope still arrives, with labels_available
    false and every label list empty, and the picker hides its filter.
    """
    if include_labels:
        # The registry read is independent of /api/states, so overlap them —
        # an unreachable or slow registry must not add to how long the picker
        # waits for its entities.
        states, registry = await asyncio.gather(
            ha_client.get_states(),
            ha_client.get_label_registry(),
            return_exceptions=True,
        )
        if isinstance(states, BaseException):
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Home Assistant unreachable")
        if isinstance(registry, BaseException):
            registry = None
    else:
        registry = None
        try:
            states = await ha_client.get_states()
        except Exception:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Home Assistant unreachable")

    # Only return entities whose domain guests can either control or view.
    entity_labels = (registry or {}).get("entity_labels", {})
    entities = [
        {
            "entity_id": s["entity_id"],
            "friendly_name": s.get("attributes", {}).get("friendly_name", s["entity_id"]),
            "domain": domain,
            "state": s["state"],
        }
        for s in states
        if (domain := s["entity_id"].split(".")[0]) in SUPPORTED_DOMAINS
    ]
    if not include_labels:
        return entities

    for entity in entities:
        entity["labels"] = entity_labels.get(entity["entity_id"], [])
    return {
        "entities": entities,
        "labels": (registry or {}).get("labels", []),
        "labels_available": registry is not None,
    }
