"""Home Assistant client: REST API calls + WebSocket fan-out for SSE."""
import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from urllib.parse import urlparse, urlunparse

import httpx
import websockets
import websockets.exceptions

from app import database as db
from app.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Named constants (L-30)
# ---------------------------------------------------------------------------
HTTP_TIMEOUT = 10
EVENT_TIMEOUT = 3
QUEUE_SIZE = 64
WS_PING_INTERVAL = 30
WS_BACKOFF_INIT = 2
WS_BACKOFF_MAX = 60
MAX_AUTH_RETRIES = 5
# Camera MJPEG is an intentionally long-lived response, so the 10s read timeout
# that suits the REST calls would kill it. Connect/write stay bounded.
CAMERA_STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)

# ---------------------------------------------------------------------------
# Persistent HTTP client
# ---------------------------------------------------------------------------
_client: httpx.AsyncClient | None = None


def _require_client() -> httpx.AsyncClient:
    """Return the persistent client, or raise if not initialized."""
    if _client is None:
        raise RuntimeError("HA client not initialized — call init_client() first")
    return _client


def init_client() -> None:
    global _client
    if _client is not None:
        return  # idempotent — don't orphan existing client
    base = settings.ha_base_url.rstrip("/")
    _client = httpx.AsyncClient(
        base_url=base,
        headers={
            "Authorization": f"Bearer {settings.ha_token}",
            "Content-Type": "application/json",
        },
        timeout=HTTP_TIMEOUT,
    )


async def close_client() -> None:
    global _client
    if _client:
        await _client.aclose()
        _client = None


# ---------------------------------------------------------------------------
# SSE subscription registry + entity cache
# ---------------------------------------------------------------------------
# token_id -> set of asyncio.Queue[dict]
_subscriptions: dict[str, set[asyncio.Queue]] = {}
_entity_cache: dict[str, set[str]] = {}
_sub_lock = asyncio.Lock()


async def subscribe(token_id: str) -> asyncio.Queue:
    """Register a new SSE queue for a token. Returns the queue."""

    q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)

    # Fetch entities OUTSIDE the lock to avoid blocking fan-out
    async with _sub_lock:
        needs_fetch = token_id not in _entity_cache
    if needs_fetch:
        entities = await db.get_token_entities(token_id)
        async with _sub_lock:
            if token_id not in _entity_cache:  # re-check after re-acquire
                _entity_cache[token_id] = set(entities)

    async with _sub_lock:
        _subscriptions.setdefault(token_id, set()).add(q)
    return q


async def unsubscribe(token_id: str, q: asyncio.Queue) -> None:
    async with _sub_lock:
        subs = _subscriptions.get(token_id)
        if subs:
            subs.discard(q)
            if not subs:
                del _subscriptions[token_id]
                _entity_cache.pop(token_id, None)


async def invalidate_entity_cache(token_id: str) -> None:
    """Re-populate cache if token has active SSE subscribers, else remove."""

    # Check for active subscribers outside the DB call
    async with _sub_lock:
        has_subs = token_id in _subscriptions
    if has_subs:
        try:
            entities = await db.get_token_entities(token_id)
        except Exception:
            logger.exception("Failed to refresh entity cache for %s", token_id)
            async with _sub_lock:
                _entity_cache.pop(token_id, None)
            return
        async with _sub_lock:
            if token_id in _subscriptions:  # re-check: may have unsubscribed
                _entity_cache[token_id] = set(entities)
            else:
                _entity_cache.pop(token_id, None)
    else:
        async with _sub_lock:
            _entity_cache.pop(token_id, None)


async def _fan_out(entity_id: str, new_state: dict) -> None:
    """Push a state_change event to all queues whose token owns the entity."""
    event = {"type": "state_change", "entity_id": entity_id, "state": new_state}
    # Deep-copy sets to avoid RuntimeError if unsubscribe modifies concurrently
    async with _sub_lock:
        snapshot = {tid: set(qs) for tid, qs in _subscriptions.items()}
        cache_snapshot = {tid: frozenset(es) for tid, es in _entity_cache.items()}
    for token_id, queues in snapshot.items():
        allowed = cache_snapshot.get(token_id, frozenset())
        if entity_id in allowed:
            for q in queues:
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    pass  # slow consumer; drop event


async def broadcast_token_expired(token_id: str) -> None:
    """Push token_expired event to all SSE connections for a token."""
    event = {"type": "token_expired"}
    async with _sub_lock:
        queues = set(_subscriptions.get(token_id, set()))
    for q in queues:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


# ---------------------------------------------------------------------------
# REST helpers (M-4: retry on transient HTTP errors)
# ---------------------------------------------------------------------------

async def _retry_http(coro_factory, retries=2, backoff_init=1):
    """Retry an HTTP call on transient failures."""
    for attempt in range(retries + 1):
        try:
            return await coro_factory()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500 or attempt == retries:
                raise
            logger.warning(
                "HA HTTP %d, retrying in %ds…",
                exc.response.status_code,
                backoff_init * (attempt + 1),
            )
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            if attempt == retries:
                raise
            logger.warning("HA HTTP error: %s, retrying in %ds…", exc, backoff_init * (attempt + 1))
        await asyncio.sleep(backoff_init * (attempt + 1))


async def get_states() -> list[dict]:
    async def _do():
        resp = await _require_client().get("/api/states")
        resp.raise_for_status()
        return resp.json()
    return await _retry_http(_do)


async def call_service(domain: str, service: str, data: dict) -> Any:
    async def _do():
        resp = await _require_client().post(f"/api/services/{domain}/{service}", json=data)
        resp.raise_for_status()
        return resp.json()
    return await _retry_http(_do)


async def fire_event(event_type: str, data: dict) -> Any:
    resp = await _require_client().post(
        f"/api/events/{event_type}",
        json=data,
        timeout=EVENT_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


async def logbook_log(data: dict) -> Any:
    resp = await _require_client().post(
        "/api/services/logbook/log",
        json=data,
        timeout=EVENT_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Camera proxying
# ---------------------------------------------------------------------------
# Both helpers take an entity_id that the CALLER must already have validated
# against the token allowlist and the camera entity-id regex. Nothing here
# re-checks it — these functions hold the privileged HA token.

async def camera_snapshot(entity_id: str) -> tuple[bytes, str]:
    """Fetch a single still frame for one camera entity."""
    async def _do():
        resp = await _require_client().get(f"/api/camera_proxy/{entity_id}")
        resp.raise_for_status()
        return resp

    resp = await _retry_http(_do)
    return resp.content, resp.headers.get("content-type", "image/jpeg")


@asynccontextmanager
async def camera_stream(entity_id: str) -> AsyncIterator[tuple[str, AsyncIterator[bytes]]]:
    """Open HA's MJPEG stream for one camera.

    Yields (content_type, raw byte iterator). The upstream connection stays open
    for the lifetime of the context, so the caller must close it when the guest
    disconnects or the connection leaks.
    """
    client = _require_client()
    async with client.stream(
        "GET",
        f"/api/camera_proxy_stream/{entity_id}",
        timeout=CAMERA_STREAM_TIMEOUT,
    ) as resp:
        resp.raise_for_status()
        ctype = resp.headers.get(
            "content-type", "multipart/x-mixed-replace; boundary=--frameboundary"
        )
        yield ctype, resp.aiter_raw()


# ---------------------------------------------------------------------------
# Home zone — read by the per-entity proximity gate
# ---------------------------------------------------------------------------
# zone.home moves about as often as the house does, so a short cache keeps a
# gated tap off HA's REST API without making a relocation take a restart to
# apply. Only successful reads are cached — while HA is unreachable every gated
# command retries, which the proximity failure budget in the router bounds.
HOME_ZONE_CACHE_TTL = 300

_home_zone: dict[str, float] | None = None
_home_zone_ts: float = 0.0


async def get_home_zone() -> dict[str, float] | None:
    """Return zone.home as {"latitude", "longitude", "radius"}, or None.

    None means "cannot be verified": zone.home absent, missing or non-numeric
    attributes, or HA unreachable. The proximity gate treats None as a refusal,
    so nothing here may substitute a default — there is no safe guess for where
    a house is.
    """
    global _home_zone, _home_zone_ts
    now = time.monotonic()
    if _home_zone is not None and (now - _home_zone_ts) < HOME_ZONE_CACHE_TTL:
        return _home_zone

    try:
        resp = await _require_client().get("/api/states/zone.home")
        resp.raise_for_status()
        attrs = resp.json().get("attributes") or {}
    except Exception:
        logger.warning("Could not read zone.home for the proximity check")
        return None

    try:
        zone = {
            "latitude": float(attrs["latitude"]),
            "longitude": float(attrs["longitude"]),
            "radius": float(attrs["radius"]),
        }
    except (KeyError, TypeError, ValueError):
        logger.warning("zone.home has no usable latitude/longitude/radius")
        return None

    _home_zone, _home_zone_ts = zone, now
    return zone


async def validate_connectivity() -> None:
    """Called at startup — raises on failure."""
    resp = await _require_client().get("/api/")
    resp.raise_for_status()
    logger.info("Home Assistant connectivity validated.")


# ---------------------------------------------------------------------------
# WebSocket listener — single persistent connection, fans out to SSE queues
# ---------------------------------------------------------------------------

_ws_task: asyncio.Task | None = None
_msg_id = 1
_ws_healthy: bool = False

# H-5: Store background task refs to prevent GC and log errors
_bg_tasks: set[asyncio.Task] = set()


def _task_done(task: asyncio.Task) -> None:
    _bg_tasks.discard(task)
    if not task.cancelled() and task.exception():
        logger.error("Fan-out task failed: %s", task.exception())


def _build_ws_url() -> str:
    parsed = urlparse(settings.ha_base_url.rstrip("/"))
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunparse(parsed._replace(scheme=scheme)) + "/api/websocket"


# L-5: Removed module-level _ws_url — built lazily inside _ws_listener


async def _broadcast_reconnected() -> None:
    """Push reconnected event to all SSE connections so they refetch state."""
    event = {"type": "reconnected"}
    async with _sub_lock:
        all_queues = [q for qs in _subscriptions.values() for q in qs]
    for q in all_queues:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


async def _ws_listener() -> None:
    global _msg_id, _ws_healthy
    ws_url = _build_ws_url()
    backoff = WS_BACKOFF_INIT
    while True:
        try:
            logger.info("Connecting to HA WebSocket at %s", ws_url)
            async with websockets.connect(ws_url, ping_interval=WS_PING_INTERVAL) as ws:
                backoff = WS_BACKOFF_INIT

                # Phase 1: auth
                raw = await ws.recv()
                msg = json.loads(raw)
                if msg.get("type") != "auth_required":
                    logger.warning("Unexpected WS message (expected auth_required): %s", msg.get("type"))
                    continue  # reconnect

                await ws.send(json.dumps({"type": "auth", "access_token": settings.ha_token}))
                raw = await ws.recv()
                msg = json.loads(raw)
                if msg.get("type") != "auth_ok":
                    logger.critical("HA WebSocket auth failed — check HA_TOKEN: %s", msg)
                    _ws_healthy = False
                    return  # Permanent — bad token; don't retry

                # Phase 2: subscribe to state_changed events
                _msg_id = 1
                await ws.send(json.dumps({
                    "id": _msg_id,
                    "type": "subscribe_events",
                    "event_type": "state_changed",
                }))
                raw = await ws.recv()
                msg = json.loads(raw)
                if not msg.get("success"):
                    logger.error("HA subscribe failed: %s", msg)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, WS_BACKOFF_MAX)
                    continue  # retry

                _ws_healthy = True
                logger.info("HA WebSocket subscribed to state_changed events.")

                # Broadcast reconnected event for SSE clients to refetch
                await _broadcast_reconnected()

                # Phase 3: fan out events
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    if msg.get("type") != "event":
                        continue

                    event_data = msg.get("event", {}).get("data", {})
                    new_state = event_data.get("new_state")
                    if not new_state:
                        continue

                    entity_id = new_state.get("entity_id", "")
                    task = asyncio.create_task(_fan_out(entity_id, new_state))
                    _bg_tasks.add(task)
                    task.add_done_callback(_task_done)

        except websockets.exceptions.ConnectionClosed:
            logger.warning("HA WebSocket closed, reconnecting in %ds…", backoff)
        except OSError as exc:
            logger.warning("HA WebSocket OSError: %s — reconnecting in %ds…", exc, backoff)
        except Exception as exc:
            logger.exception("HA WebSocket unexpected error — reconnecting in %ds…", backoff)

        _ws_healthy = False
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, WS_BACKOFF_MAX)


def is_ws_healthy() -> bool:
    """Return True if the WebSocket listener is connected and subscribed."""
    return _ws_healthy and _ws_task is not None and not _ws_task.done()


async def start_ws_listener() -> None:
    global _ws_task
    _ws_task = asyncio.create_task(_ws_listener())
    logger.info("HA WebSocket listener task started.")


async def stop_ws_listener() -> None:
    global _ws_task
    if _ws_task:
        _ws_task.cancel()
        try:
            await _ws_task
        except asyncio.CancelledError:
            pass
        _ws_task = None


# ---------------------------------------------------------------------------
# WebSocket request/response — one throwaway connection per command
# ---------------------------------------------------------------------------
# The listener above is a one-way subscription: it owns its recv order and every
# guest's live updates ride on it. Rather than thread a pending-futures map
# through that state machine, a command opens its own connection, authenticates,
# asks, reads the reply and closes. The SSE fan-out is untouched, the failure
# modes are local to the caller, and the extra connect only happens on a cache
# miss.
WS_COMMAND_TIMEOUT = 10


class WSCommandError(Exception):
    """A WS command that did not come back successfully.

    `code` is Home Assistant's own error code when it sent one ("unauthorized",
    "unknown_command", …), or one of the local codes below when the failure
    happened before HA could answer.
    """

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


# Local stand-ins for the codes HA never gets to send
WS_CODE_AUTH_FAILED = "auth_failed"
WS_CODE_TIMEOUT = "timeout"
WS_CODE_CONNECTION = "connection"


async def ws_command(command: dict) -> Any:
    """Send one command over a short-lived WS connection, return its result.

    Raises WSCommandError for every failure — auth refused, `success: false`,
    timeout, or the connection dropping before the reply arrives. Callers decide
    what a failure means; nothing here retries.
    """
    ws_url = _build_ws_url()
    msg_id = 1
    try:
        async with asyncio.timeout(WS_COMMAND_TIMEOUT):
            # ping_interval is off: the connection lives for one round trip, so
            # the keepalive would never fire and only risks racing the close.
            async with websockets.connect(ws_url, ping_interval=None) as ws:
                msg = json.loads(await ws.recv())
                if msg.get("type") != "auth_required":
                    raise WSCommandError(
                        f"Unexpected WS greeting: {msg.get('type')}", WS_CODE_CONNECTION
                    )

                await ws.send(json.dumps({"type": "auth", "access_token": settings.ha_token}))
                msg = json.loads(await ws.recv())
                if msg.get("type") != "auth_ok":
                    raise WSCommandError(
                        f"HA WebSocket auth failed: {msg.get('message') or msg.get('type')}",
                        WS_CODE_AUTH_FAILED,
                    )

                await ws.send(json.dumps({"id": msg_id, **command}))
                # HA may interleave other messages; take the first result for us.
                async for raw in ws:
                    try:
                        reply = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if reply.get("type") == "result" and reply.get("id") == msg_id:
                        break
                else:
                    raise WSCommandError(
                        "HA closed the WebSocket before answering", WS_CODE_CONNECTION
                    )

                if not reply.get("success"):
                    error = reply.get("error") or {}
                    raise WSCommandError(
                        error.get("message") or "HA refused the command",
                        error.get("code"),
                    )
                return reply.get("result")
    except TimeoutError as exc:
        raise WSCommandError(
            f"HA did not answer {command.get('type')} within {WS_COMMAND_TIMEOUT}s",
            WS_CODE_TIMEOUT,
        ) from exc
    except (websockets.exceptions.WebSocketException, OSError) as exc:
        raise WSCommandError(f"HA WebSocket error: {exc}", WS_CODE_CONNECTION) from exc


# ---------------------------------------------------------------------------
# Entity + label registries — the only source of HA labels
# ---------------------------------------------------------------------------
# REST /api/states carries state and attributes, not registry data, so labels
# are only reachable over the WS API. Verified against HA core (dev):
# `config/entity_registry/list` returns EntityRegistryEntry.as_partial_dict,
# which includes "entity_id" and "labels" (a list of label ids), and
# `config/label_registry/list` returns {"label_id", "name", "color", "icon",
# "description", …}. Neither carries @require_admin — only the create/update/
# remove variants do — so the non-admin long-lived tokens that cannot POST
# /api/events/ (see app/routers/guest.py) can still read labels.
#
# HA can still refuse or not offer them (a core old enough to predate the label
# registry answers "unknown_command"), and the picker has to work either way, so
# every failure here degrades to "labels unavailable" instead of propagating.
#
# Labels are edited by hand in the HA UI, so they change on human timescales. A
# 10-minute TTL means a newly created label shows up in the picker on its own
# within one coffee break, while a burst of modal opens costs one registry read.
REGISTRY_CACHE_TTL = 600

# A refusal is permanent for this token, exactly like the activity-event refusal
# in app/routers/guest.py — same latch, same hourly re-probe so fixing HA takes
# effect without a restart, same "explain it once" rule.
REGISTRY_DENIED_RETRY_SECONDS = 3600

# error code -> the one message an admin gets when it latches
_REGISTRY_DENIED_HELP = {
    "unauthorized": (
        "Home Assistant refused to list its entity/label registry and will refuse "
        "every later read: the user behind HA_TOKEN is not permitted to read it. "
        "HAPass has stopped asking, so the admin entity picker has no label filter. "
        "Everything else — the entity list itself, guest access, tokens — is "
        "unaffected. Fix the token's permissions and HAPass picks labels back up "
        "within an hour, no restart needed."
    ),
    "unknown_command": (
        "Home Assistant does not offer config/entity_registry/list or "
        "config/label_registry/list, so HAPass cannot read labels and the admin "
        "entity picker has no label filter. Nothing else is affected. This usually "
        "means a Home Assistant too old to have the label registry."
    ),
    WS_CODE_AUTH_FAILED: (
        "Home Assistant rejected HA_TOKEN on the WebSocket API, so HAPass cannot "
        "read labels and the admin entity picker has no label filter. Replace "
        "HA_TOKEN with a valid long-lived access token."
    ),
}

_registry_cache: dict[str, Any] | None = None
_registry_cache_ts: float = 0.0
# time.monotonic() of the refusal that latched label reads off
_registry_denied_at: float | None = None


def _registry_reads_open() -> bool:
    """False while registry reads are latched off by a refusal already explained."""
    if _registry_denied_at is None:
        return True
    return (time.monotonic() - _registry_denied_at) >= REGISTRY_DENIED_RETRY_SECONDS


def _note_registry_failure(exc: WSCommandError) -> None:
    """Log one registry-read failure, latching the permanent ones."""
    global _registry_denied_at
    help_message = _REGISTRY_DENIED_HELP.get(exc.code or "")
    if help_message:
        # Only the move into refused is logged; a re-probe that comes back
        # refused again tells the admin nothing new.
        first_refusal = _registry_denied_at is None
        _registry_denied_at = time.monotonic()
        if first_refusal:
            logger.error(help_message)
        return
    logger.warning("Could not read the HA entity/label registry: %s", exc)


def _note_registry_success() -> None:
    """Unlatch reads HA has started answering again."""
    global _registry_denied_at
    if _registry_denied_at is not None:
        _registry_denied_at = None
        logger.info("Home Assistant is answering HAPass registry reads again.")


async def get_label_registry() -> dict[str, Any] | None:
    """Return the label catalogue plus entity -> label ids, or None.

    None means "labels cannot be read" — refused, unsupported, or HA unreachable.
    Callers must treat it as "this deployment has no label filter", never as
    "no entity has labels". Only successful reads are cached.
    """
    global _registry_cache, _registry_cache_ts
    now = time.monotonic()
    if _registry_cache is not None and (now - _registry_cache_ts) < REGISTRY_CACHE_TTL:
        return _registry_cache
    if not _registry_reads_open():
        return None

    try:
        labels = await ws_command({"type": "config/label_registry/list"})
        entries = await ws_command({"type": "config/entity_registry/list"})
    except WSCommandError as exc:
        _note_registry_failure(exc)
        return None
    except Exception as exc:  # malformed payload, anything unforeseen
        logger.warning("Could not read the HA entity/label registry: %s", exc)
        return None

    try:
        catalogue = sorted(
            (
                {
                    "label_id": label["label_id"],
                    "name": label.get("name") or label["label_id"],
                    "color": label.get("color"),
                    "icon": label.get("icon"),
                }
                for label in labels or []
            ),
            key=lambda label: label["name"].casefold(),
        )
        # Only entities that actually carry a label get an entry, so the payload
        # stays proportional to label use rather than to registry size.
        entity_labels = {
            entry["entity_id"]: list(entry["labels"])
            for entry in entries or []
            if entry.get("labels")
        }
    except (KeyError, TypeError) as exc:
        logger.warning("HA registry reply was not in the expected shape: %s", exc)
        return None

    _note_registry_success()
    _registry_cache = {"labels": catalogue, "entity_labels": entity_labels}
    _registry_cache_ts = now
    return _registry_cache
