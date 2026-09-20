"""Guest API router: PWA shell, state, SSE, and command proxy."""
# Security note: The slug in the URL acts as a bearer token — knowing the
# slug grants access. CSRF is mitigated by the fact that all state-changing
# operations require the slug in the URL path (not a cookie). The admin
# dashboard uses SameSite=strict cookies for CSRF protection.
import asyncio
import ipaddress
from contextlib import AsyncExitStack
import json
import logging
import re
import time
from typing import AsyncIterator

import httpx
from fastapi import APIRouter, BackgroundTasks, Form, HTTPException, Path, Request, status
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates

from app import database as db
from app import guest_pin
from app import ha_client
from app import proximity
from app.config import settings
from app.context import base_context
from app.models import (
    ALLOWED_SERVICES,
    CommandRequest,
    FORBIDDEN_DATA_KEYS,
    NEVER_EXPIRES_SECONDS,
    validate_light_color,
)
from app.rate_limiter import rate_limiter

router = APIRouter(prefix="/g")
logger = logging.getLogger(__name__)

# L-31: Named constant for SSE keepalive interval
SSE_KEEPALIVE_SECONDS = 25

# Global rate limits for the guest command proxy, as (window_seconds, max_requests)
# pairs per token — a request has to pass every one of them.
# Hardcoded — no comparable self-hosted app exposes per-user rate limits.
#
# A single per-minute cap cannot serve both cases here. The light colour wheel
# streams throttled updates for as long as the guest drags it, so the short
# window has to be generous: at ~4 updates/second, 300/min covers a solid minute
# of dragging plus the taps interleaved with it. A cap that loose is the wrong
# long-run budget though, so the hour window carries the real ceiling — 3000/hour
# is ten minutes at the burst rate, and clamps anything scripted to under
# 1 req/s averaged out.
COMMAND_BURST_RPM = 300
COMMAND_SUSTAINED_RPH = 3000
COMMAND_LIMITS = ((60.0, COMMAND_BURST_RPM), (3600.0, COMMAND_SUSTAINED_RPH))

# Camera stills are cheap and the UI refreshes thumbnails on a timer, so they get
# their own, looser budget under a separate limiter key — a guest watching a camera
# must not burn the command allowance that controls their lights.
CAMERA_SNAPSHOT_RPM = 120

# Each live MJPEG view holds one upstream connection to HA open for its whole
# lifetime, so this is capped per token rather than rate-limited per minute.
# Cameras stream for as long as the guest page is open, so the real consumption
# is (cameras on the token) x (open tabs) — 2 would be exhausted by a single
# 2-camera page and 429 the guest's second device.
MAX_STREAMS_PER_TOKEN = 8
_active_streams: dict[str, int] = {}
_stream_lock = asyncio.Lock()

# Budget for proximity checks that come back refused, per token, on top of the
# ordinary command limits above. Two reasons it exists:
#
# A refusal is otherwise a free oracle — a caller could binary-search the home
# coordinates out of the gate by watching which lat/long pairs come back 403.
# This slows that to a crawl rather than closing it; the position of a house
# whose guest link you already hold is not a secret worth a tighter cap.
#
# And a guest who really is away stops after a handful of taps with "too many
# location checks" instead of retrying into the main command budget forever.
#
# Only refusals are recorded, so a guest who is actually at the property never
# touches this. Someone holding the slug can exhaust it to keep the real guest
# out — but they can already exhaust COMMAND_LIMITS and deny every entity on the
# token, so it is not a new exposure.
PROXIMITY_FAILURE_LIMITS = ((60.0, 5), (3600.0, 30))

# Brute-force budget for PIN entry, as (window_seconds, max_attempts) pairs.
# Two keys, both of which an attempt has to pass, because neither alone works:
#
# Keyed on the token only, an attacker who rotates IPs still hits one shared
# ceiling — but they can also spend that ceiling to lock the real guest out.
# Keyed on the IP only, rotating addresses evades the limit entirely, and one
# NAT'd household shares a budget across unrelated tokens.
#
# So: a tight per-(token, IP) budget catches the ordinary case, and a looser
# per-token budget bounds total guesses no matter how many addresses are used.
# The per-token ceiling is deliberately well above what a guest fumbling their
# PIN needs, and still caps a 4-digit space at ~2400 guesses/day — the same
# DoS-vs-brute-force trade the command limiter already makes per token.
PIN_ATTEMPT_LIMITS_PER_IP = ((60.0, 5), (3600.0, 20))
PIN_ATTEMPT_LIMITS_PER_TOKEN = ((60.0, 15), (3600.0, 100))

# entity_id arrives in a URL path here (it does not anywhere else in this app) and
# is interpolated into the upstream HA request, so it is matched against an exact
# shape rather than merely checked for membership.
_CAMERA_ENTITY_RE = re.compile(r"^camera\.[a-z0-9_]+$")

# L-8: Whitelist of allowed SSE event types
_ALLOWED_SSE_EVENTS = {"state_change", "token_expired", "reconnected"}

# M-27: Simple TTL cache for HA state list
_states_cache: list[dict] | None = None
_states_cache_ts: float = 0
STATE_CACHE_TTL = 30  # seconds
ACTIVITY_EVENT_TYPE = "ha_pass_activity"
ACTIVITY_SCHEMA_VERSION = 1
PAGE_LOAD_EVENT_DEBOUNCE_SECONDS = 30
_page_load_activity_ts: dict[str, float] = {}


async def _get_cached_states() -> list[dict]:
    global _states_cache, _states_cache_ts
    now = time.monotonic()
    if _states_cache is not None and (now - _states_cache_ts) < STATE_CACHE_TTL:
        return _states_cache
    _states_cache = await ha_client.get_states()
    _states_cache_ts = now
    return _states_cache


templates = Jinja2Templates(directory="templates")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client_ip(request: Request) -> str:
    """Extract the client IP from X-Forwarded-For (set by reverse proxy).

    IMPORTANT: HAPass MUST be deployed behind a reverse proxy (Caddy, nginx,
    Cloudflare Tunnel, etc.) that overwrites the X-Forwarded-For header with the
    true client IP. Without this, clients can spoof their IP to bypass allowlists.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _enforce_ip_allowlist(row, request: Request) -> None:
    if not row["ip_allowlist"]:
        return
    client_ip = _client_ip(request)
    allowed_cidrs: list[str] = json.loads(row["ip_allowlist"])
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid client IP")
    if not any(addr in ipaddress.ip_network(cidr, strict=False) for cidr in allowed_cidrs):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="IP not allowed")


def _pin_gate_ok(row, request: Request) -> bool:
    """True if this token carries no PIN, or this request already proved it.

    A token with no PIN — the default — never reaches the signature check, so
    nothing about those requests changes.
    """
    pin_hash = row["pin_hash"]
    if not pin_hash:
        return True
    return guest_pin.verify_session(
        request.cookies.get(guest_pin.SESSION_COOKIE), row["id"], pin_hash
    )


def _is_https(request: Request) -> bool:
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    return request.url.scheme == "https" or forwarded_proto == "https"


def _pin_cookie_path(request: Request, slug: str) -> str:
    """Scope the PIN cookie to one token's own URL prefix.

    Under HA ingress the app is mounted below /api/hassio_ingress/<token>, so the
    path has to carry that prefix or the browser never sends the cookie back.
    Narrowing to /g/<slug> also keeps a session for one token off the wire on
    another token's requests — cookie paths match on whole segments, so /g/abc
    is not sent for /g/abcdef. The HMAC binding in guest_pin is what actually
    enforces the scoping; this just stops the cookie travelling needlessly.
    """
    return f"{request.state.ingress_path}/g/{slug}"


async def _validate_token(slug: str, request: Request):
    """Load and validate a token by slug. Raises HTTP 410 on any issue.

    The PIN gate lives here rather than in each handler so every guest endpoint
    that reads state or performs an action inherits it, including ones added
    later. Gating only the HTML page would leave /state, /stream, /command and
    both camera endpoints reachable with nothing but the slug — the camera pair
    being the worst of it, since those relay live frames.
    """
    row = await db.get_token_by_slug(slug)
    if not row:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Access unavailable")

    now = int(time.time())
    if row["revoked"] or row["expires_at"] <= now:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Access unavailable")

    _enforce_ip_allowlist(row, request)

    if not _pin_gate_ok(row, request):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="PIN required")

    return row


async def _refuse_proximity(token_id: str, status_code: int, detail: str) -> None:
    """Record a refused proximity check and raise, or raise 429 once it is spent."""
    if not await rate_limiter.check_multi(f"prox:{token_id}", PROXIMITY_FAILURE_LIMITS):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many location checks — please wait a minute",
        )
    raise HTTPException(status_code=status_code, detail=detail)


async def _enforce_proximity(row, body: CommandRequest) -> None:
    """Refuse a gated entity's command unless a fresh fix puts the guest at home.

    Only entities the admin marked reach any of this, so an ungated entity on
    the same token neither needs a location nor waits on one — the lookup is a
    membership test and returns immediately when nothing is gated.

    Fails closed at every step: no fix, a stale fix, or a zone.home that cannot
    be read all refuse. A gate that opens when it cannot verify is not a gate,
    and the cost of the strict side is an admin noticing their door button stops
    working while HA is unreachable.

    Soft by nature — see app/proximity.py. The coordinates are self-reported, so
    this is friction for a casual guest, not evidence anyone is at the door.
    """
    gated = await db.get_proximity_entity_ids(row["id"])
    if body.entity_id not in gated:
        return

    token_id = row["id"]
    loc = body.location
    if loc is None:
        await _refuse_proximity(
            token_id,
            status.HTTP_400_BAD_REQUEST,
            "This control needs your location",
        )

    if not proximity.fix_is_fresh(loc.timestamp, time.time()):
        await _refuse_proximity(
            token_id,
            status.HTTP_400_BAD_REQUEST,
            "Your location is out of date — try again",
        )

    zone = await ha_client.get_home_zone()
    if zone is None:
        await _refuse_proximity(
            token_id,
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Can't check your location right now",
        )

    if not proximity.is_within_zone(loc.latitude, loc.longitude, zone):
        await _refuse_proximity(
            token_id,
            status.HTTP_403_FORBIDDEN,
            "You need to be at the property to use this",
        )


async def _fire_activity_event(payload: dict) -> None:
    try:
        await ha_client.fire_event(ACTIVITY_EVENT_TYPE, payload)
    except Exception as exc:
        logger.warning("Failed to emit HA activity event: %s", exc)
    try:
        await ha_client.logbook_log(_logbook_payload(payload))
    except Exception as exc:
        logger.warning("Failed to write HA logbook activity: %s", exc)


def _logbook_payload(payload: dict) -> dict:
    token_label = payload["token_label"]
    if payload["activity"] == "command":
        target_entity_id = payload["target_entity_id"]
        data = {
            "name": "HAPass",
            "message": f"{token_label} used {payload['service']} on {target_entity_id}",
            "entity_id": target_entity_id,
        }
        if target_entity_id and "." in target_entity_id:
            data["domain"] = target_entity_id.split(".", 1)[0]
        return data
    return {
        "name": "HAPass",
        "message": f"{token_label} opened guest link",
    }


def _activity_payload(
    row,
    activity: str,
    target_entity_id: str | None = None,
    service: str | None = None,
) -> dict:
    return {
        "schema_version": ACTIVITY_SCHEMA_VERSION,
        "activity": activity,
        "token_label": row["label"],
        "target_entity_id": target_entity_id,
        "service": service,
    }


def _schedule_activity_event(background_tasks: BackgroundTasks, payload: dict) -> None:
    background_tasks.add_task(_fire_activity_event, payload)


def _schedule_page_load_activity(background_tasks: BackgroundTasks, row) -> None:
    now = time.monotonic()
    cutoff = now - PAGE_LOAD_EVENT_DEBOUNCE_SECONDS
    for token_id, last_emitted in list(_page_load_activity_ts.items()):
        if last_emitted < cutoff:
            del _page_load_activity_ts[token_id]
    token_id = row["id"]
    last_emitted = _page_load_activity_ts.get(token_id)
    if last_emitted is not None and (now - last_emitted) < PAGE_LOAD_EVENT_DEBOUNCE_SECONDS:
        return
    _page_load_activity_ts[token_id] = now
    _schedule_activity_event(background_tasks, _activity_payload(row, "page_load"))


# ---------------------------------------------------------------------------
# PWA shell
# ---------------------------------------------------------------------------

@router.get("/{slug}", response_class=HTMLResponse)
async def guest_pwa(background_tasks: BackgroundTasks, request: Request, slug: str = Path(max_length=64)):
    row = await db.get_token_by_slug(slug)
    expired = False
    if not row or row["revoked"] or row["expires_at"] <= int(time.time()):
        expired = True

    if expired:
        ctx = base_context(request)
        ctx.update({"slug": slug, "contact_message": settings.contact_message})
        return templates.TemplateResponse(request, "expired.html", ctx, status_code=410)

    try:
        _enforce_ip_allowlist(row, request)
    except HTTPException as exc:
        ctx = base_context(request)
        ctx.update({"slug": slug, "contact_message": settings.contact_message})
        return templates.TemplateResponse(request, "expired.html", ctx, status_code=exc.status_code)

    # Locked tokens get the PIN screen instead of the app. Nothing is touched or
    # logged yet — an unanswered prompt is not an access, the same way a request
    # blocked by the IP allowlist above is not.
    if not _pin_gate_ok(row, request):
        ctx = base_context(request)
        ctx.update({"slug": slug, "contact_message": settings.contact_message})
        return templates.TemplateResponse(request, "pin_entry.html", ctx)

    await db.touch_token(row["id"])
    await db.log_access(
        token_id=row["id"],
        event_type="page_load",
        ip_address=_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
    )
    _schedule_page_load_activity(background_tasks, row)
    ctx = base_context(request)
    ctx.update({
        "slug": slug,
        "label": row["label"],
        "expires_at": row["expires_at"],
        "contact_message": settings.contact_message,
        "never_expires": NEVER_EXPIRES_SECONDS,
        # Decided here, not in the browser: the template only emits the
        # geolocation block when this is true, so a token with nothing gated
        # renders a page that never mentions the API and can never prompt.
        "requires_location": bool(await db.get_proximity_entity_ids(row["id"])),
    })
    return templates.TemplateResponse(request, "guest_pwa.html", ctx)


# ---------------------------------------------------------------------------
# PIN entry
# ---------------------------------------------------------------------------

@router.post("/{slug}/pin", response_class=HTMLResponse)
async def guest_pin_submit(
    request: Request,
    slug: str = Path(max_length=64),
    # No max_length here on purpose: a Form() constraint failure returns a 422
    # whose body echoes the rejected `input`, which would put the PIN in a
    # response. Length is checked below, where the answer is a generic error.
    pin: str = Form(default=""),
):
    """Check a submitted PIN and, on success, hand back a session cookie.

    POST rather than a query parameter so the PIN never reaches browser history,
    a Referer header, or the reverse proxy's access log.
    """
    row = await db.get_token_by_slug(slug)
    if not row or row["revoked"] or row["expires_at"] <= int(time.time()):
        ctx = base_context(request)
        ctx.update({"slug": slug, "contact_message": settings.contact_message})
        return templates.TemplateResponse(request, "expired.html", ctx, status_code=410)

    try:
        _enforce_ip_allowlist(row, request)
    except HTTPException as exc:
        ctx = base_context(request)
        ctx.update({"slug": slug, "contact_message": settings.contact_message})
        return templates.TemplateResponse(request, "expired.html", ctx, status_code=exc.status_code)

    pin_hash = row["pin_hash"]
    if not pin_hash:
        # Nothing to unlock. Same redirect a correct PIN gets, so a guest sitting
        # on a bookmarked PIN page still lands on the app after an admin clears
        # the PIN. That the token has none is already plain from GET /g/<slug>.
        return RedirectResponse(
            url=f"{request.state.ingress_path}/g/{slug}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    # Per-(token, IP) first: a single address that has already burned its own
    # budget is turned away without also spending the token-wide one.
    ip_ok = await rate_limiter.check_multi(
        f"pin:{row['id']}:{_client_ip(request)}", PIN_ATTEMPT_LIMITS_PER_IP
    )
    if not ip_ok or not await rate_limiter.check_multi(
        f"pin:{row['id']}", PIN_ATTEMPT_LIMITS_PER_TOKEN
    ):
        ctx = base_context(request)
        ctx.update({
            "slug": slug,
            "contact_message": settings.contact_message,
            "error": "Too many attempts — please wait a minute and try again.",
        })
        return templates.TemplateResponse(
            request, "pin_entry.html", ctx,
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    if not guest_pin.is_valid_pin(pin) or not await guest_pin.verify_pin(pin, pin_hash):
        ctx = base_context(request)
        ctx.update({
            "slug": slug,
            "contact_message": settings.contact_message,
            "error": "Incorrect PIN",
        })
        return templates.TemplateResponse(
            request, "pin_entry.html", ctx,
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    value, max_age = guest_pin.issue_session(row["id"], pin_hash, row["expires_at"])
    response = RedirectResponse(
        url=f"{request.state.ingress_path}/g/{slug}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    response.set_cookie(
        guest_pin.SESSION_COOKIE,
        value,
        httponly=True,
        # Lax, not strict: guest links are opened from a text message or an
        # email, and a strict cookie is withheld on that first cross-site
        # navigation — the guest would be re-prompted every single time. Lax
        # still withholds it from cross-site POSTs, so a forged command from
        # another origin fails the gate.
        samesite="lax",
        secure=_is_https(request),
        max_age=max_age,
        path=_pin_cookie_path(request, slug),
    )
    return response


# ---------------------------------------------------------------------------
# Dynamic PWA manifest
# ---------------------------------------------------------------------------

@router.get("/{slug}/manifest.json")
async def guest_manifest(request: Request, slug: str = Path(max_length=64)):
    bp = request.state.ingress_path
    manifest = {  # colors must match static/input.css
        "name": settings.app_name,
        "short_name": settings.app_name[:12],
        "description": "Temporary home controls",
        "start_url": f"{bp}/g/{slug}",
        "scope": f"{bp}/g/{slug}",
        "display": "standalone",
        "background_color": settings.brand_bg,
        "theme_color": settings.brand_primary,
        "orientation": "portrait",
        "icons": [
            {"src": f"{bp}/static/icons/icon-192.png", "sizes": "192x192",
             "type": "image/png", "purpose": "any"},
            {"src": f"{bp}/static/icons/icon-512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "any"},
            {"src": f"{bp}/static/icons/icon-maskable-192.png", "sizes": "192x192",
             "type": "image/png", "purpose": "maskable"},
            {"src": f"{bp}/static/icons/icon-maskable-512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "maskable"},
        ],
    }
    return JSONResponse(manifest)


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------

@router.get("/{slug}/state")
async def guest_state(request: Request, slug: str = Path(max_length=64)):
    row = await _validate_token(slug, request)
    entity_ids = await db.get_token_entities(row["id"])

    allowed = set(entity_ids)
    all_states = await _get_cached_states()
    states = {}
    for s in all_states:
        eid = s.get("entity_id", "")
        if eid in allowed:
            states[eid] = s
    for eid in entity_ids:
        if eid not in states:
            states[eid] = {"entity_id": eid, "state": "unavailable", "attributes": {}}

    # Presentation overrides ride alongside the states rather than being merged
    # into them, so the raw HA attributes the UI reads stay untouched. The
    # per-entity require_proximity flag comes through here too — the guest UI
    # uses it to mark which controls will ask for a location, and it is
    # false everywhere on a token with no gated entity.
    meta = await db.get_token_entity_meta(row["id"])
    return {"entities": entity_ids, "states": states, "entity_meta": meta}


# ---------------------------------------------------------------------------
# SSE stream
# ---------------------------------------------------------------------------

async def _event_generator(token_id: str, slug: str, request: Request) -> AsyncIterator[str]:
    q = await ha_client.subscribe(token_id)
    try:
        # M-5: Expose WS health in SSE connected event
        yield f"event: connected\ndata: {{\"ws_healthy\": {str(ha_client.is_ws_healthy()).lower()}}}\n\n"

        while True:
            if await request.is_disconnected():
                break

            try:
                event = await asyncio.wait_for(q.get(), timeout=SSE_KEEPALIVE_SECONDS)
                # L-8: Only forward whitelisted event types
                if event["type"] not in _ALLOWED_SSE_EVENTS:
                    continue
                yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                if event["type"] == "token_expired":
                    break
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"

    finally:
        await ha_client.unsubscribe(token_id, q)


@router.get("/{slug}/stream")
async def guest_stream(request: Request, slug: str = Path(max_length=64)):
    row = await _validate_token(slug, request)
    return StreamingResponse(
        _event_generator(row["id"], slug, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Camera proxy
# ---------------------------------------------------------------------------
# Guests never receive an HA URL or the HA token. Every frame is relayed through
# these endpoints after the same token + allowlist checks the command path uses.

async def _validate_camera(slug: str, entity_id: str, request: Request):
    """Shared gate for both camera endpoints. Order matters: token, shape, allowlist."""
    row = await _validate_token(slug, request)

    if not _CAMERA_ENTITY_RE.match(entity_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid camera entity",
        )

    entity_ids = await db.get_token_entities(row["id"])
    if entity_id not in entity_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Entity not in allowlist")

    return row


@router.get("/{slug}/camera/{entity_id}")
async def guest_camera_snapshot(
    request: Request,
    slug: str = Path(max_length=64),
    entity_id: str = Path(max_length=255),
):
    row = await _validate_camera(slug, entity_id, request)

    if not await rate_limiter.check(f"cam:{row['id']}", CAMERA_SNAPSHOT_RPM):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Rate limit exceeded")

    try:
        data, ctype = await ha_client.camera_snapshot(entity_id)
    except Exception:
        logger.warning("Camera snapshot failed for %s", entity_id)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Camera unavailable")

    return Response(content=data, media_type=ctype, headers={"Cache-Control": "no-store"})


@router.get("/{slug}/camera/{entity_id}/stream")
async def guest_camera_stream(
    request: Request,
    slug: str = Path(max_length=64),
    entity_id: str = Path(max_length=255),
):
    row = await _validate_camera(slug, entity_id, request)
    token_id = row["id"]

    async with _stream_lock:
        if _active_streams.get(token_id, 0) >= MAX_STREAMS_PER_TOKEN:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many concurrent streams",
            )
        _active_streams[token_id] = _active_streams.get(token_id, 0) + 1

    async def _release() -> None:
        async with _stream_lock:
            remaining = _active_streams.get(token_id, 1) - 1
            if remaining > 0:
                _active_streams[token_id] = remaining
            else:
                _active_streams.pop(token_id, None)

    # The upstream is opened here rather than inside the generator so the real
    # boundary from HA's Content-Type reaches the browser, and so an upstream
    # failure surfaces as 502 instead of a truncated 200.
    stack = AsyncExitStack()
    try:
        ctype, chunks = await stack.enter_async_context(ha_client.camera_stream(entity_id))
    except Exception:
        await stack.aclose()
        await _release()
        logger.warning("Camera stream failed to open for %s", entity_id)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Camera unavailable")

    async def _relay() -> AsyncIterator[bytes]:
        try:
            async for chunk in chunks:
                if await request.is_disconnected():
                    break
                yield chunk
        except Exception:
            logger.info("Camera stream ended for %s", entity_id)
        finally:
            await stack.aclose()
            await _release()

    return StreamingResponse(
        _relay(),
        media_type=ctype,
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Command proxy
# ---------------------------------------------------------------------------

@router.post("/{slug}/command")
async def guest_command(
    body: CommandRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    slug: str = Path(max_length=64),
):
    row = await _validate_token(slug, request)
    token_id = row["id"]

    allowed = await rate_limiter.check_multi(token_id, COMMAND_LIMITS)
    if not allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Rate limit exceeded")

    # L-6: Validate service format before processing
    if not re.match(r'^[a-z_]+\.[a-z_]+$', body.service) and not re.match(r'^[a-z_]+$', body.service):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid service format",
        )

    entity_ids = await db.get_token_entities(token_id)
    if body.entity_id not in entity_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Entity not in allowlist")

    entity_domain = body.entity_id.split(".")[0]

    if "." in body.service:
        svc_domain, svc_name = body.service.split(".", 1)
        if svc_domain != entity_domain:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Service domain does not match entity",
            )
    else:
        svc_name = body.service

    allowed_svc = ALLOWED_SERVICES.get(entity_domain)
    if not allowed_svc or svc_name not in allowed_svc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Service '{svc_name}' not allowed for {entity_domain}",
        )

    # Last of the authorization checks, and before the payload ones, so a
    # malformed colour on a gated entity from off-site still answers "you need
    # to be at the property" rather than confirming the payload was fine.
    await _enforce_proximity(row, body)

    # The colour wheel is the one widget that posts a structured value built
    # from raw pointer coordinates, so its payload is validated rather than
    # forwarded on trust.
    if entity_domain == "light" and svc_name == "turn_on":
        color_error = validate_light_color(body.data)
        if color_error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=color_error,
            )

    # Only entity_id and service are ever logged, so secrets a widget has to
    # pass through here — an alarm code, say — stay in transit and nowhere else.
    clean_data = {k: v for k, v in body.data.items() if k not in FORBIDDEN_DATA_KEYS}
    service_data = {**clean_data, "entity_id": body.entity_id}

    try:
        result = await ha_client.call_service(entity_domain, svc_name, service_data)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Service call failed")
    except Exception:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Service call failed")

    await db.log_access(
        token_id=token_id,
        event_type="command",
        ip_address=_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
        entity_id=body.entity_id,
        service=body.service,
    )
    _schedule_activity_event(
        background_tasks,
        _activity_payload(
            row,
            "command",
            target_entity_id=body.entity_id,
            service=f"{entity_domain}.{svc_name}",
        ),
    )

    return {"ok": True}
