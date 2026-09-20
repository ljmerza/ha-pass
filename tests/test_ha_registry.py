"""HA entity/label registry reads over the WebSocket API — upstream issue #6.

Two layers are exercised:

- ha_client.ws_command / get_label_registry against a scripted fake WebSocket,
  monkeypatching websockets.connect the way test_ha_client.py monkeypatches
  ha_client._client. The mock_ha_client fixture is no use here — it replaces the
  very functions under test.
- GET /admin/ha/entities through the real router, where mock_ha_client is the
  right tool.

Labels are a nice-to-have: every failure mode below must end with the picker
still getting its entities, just without a label filter.
"""
import asyncio
import json
import logging

import pytest
import websockets.exceptions

from app import ha_client


# ---------------------------------------------------------------------------
# A scripted stand-in for one HA WebSocket connection
# ---------------------------------------------------------------------------
# Plays HA's handshake — auth_required, then auth_ok/auth_invalid, then one
# result per command — so the tests assert against the real message shapes
# rather than a mock's call list.

AUTH_REQUIRED = {"type": "auth_required", "ha_version": "2026.9.0"}

# What config/label_registry/list and config/entity_registry/list actually
# return, trimmed to the fields this code reads. Verified against HA core:
# label_registry._entry_dict and EntityRegistryEntry.as_partial_dict.
SAMPLE_LABELS = [
    {
        "label_id": "guest_safe",
        "name": "Guest safe",
        "color": "green",
        "icon": "mdi:account",
        "description": None,
        "created_at": 1700000000.0,
        "modified_at": 1700000000.0,
    },
    {
        "label_id": "downstairs",
        "name": "Downstairs",
        "color": None,
        "icon": None,
        "description": None,
        "created_at": 1700000000.0,
        "modified_at": 1700000000.0,
    },
]

SAMPLE_ENTITY_ENTRIES = [
    {"entity_id": "light.kitchen", "labels": ["guest_safe", "downstairs"], "platform": "hue"},
    {"entity_id": "switch.patio", "labels": ["guest_safe"], "platform": "tplink"},
    {"entity_id": "light.attic", "labels": [], "platform": "hue"},
]


class FakeWebSocket:
    """Enough of the websockets client connection for one request/response."""

    def __init__(self, *, auth_ok=True, responder=None, greeting=AUTH_REQUIRED,
                 close_before_result=False, hang=False):
        self.auth_ok = auth_ok
        self.responder = responder
        self.close_before_result = close_before_result
        self.hang = hang
        self.sent: list[dict] = []
        self._outbox: list[str] = [json.dumps(greeting)]

    async def recv(self) -> str:
        if not self._outbox:
            raise websockets.exceptions.ConnectionClosedOK(None, None)
        return self._outbox.pop(0)

    async def send(self, raw: str) -> None:
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg.get("type") == "auth":
            self._outbox.append(json.dumps(
                {"type": "auth_ok", "ha_version": "2026.9.0"} if self.auth_ok
                else {"type": "auth_invalid", "message": "Invalid access token"}
            ))
            return
        if self.close_before_result:
            return  # nothing queued — the iteration below just ends
        reply = dict(self.responder(msg))
        reply.setdefault("id", msg["id"])
        reply.setdefault("type", "result")
        self._outbox.append(json.dumps(reply))

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if self.hang:
            await asyncio.Event().wait()
        if not self._outbox:
            raise StopAsyncIteration
        return self._outbox.pop(0)


class _FakeConnect:
    def __init__(self, ws):
        self._ws = ws

    async def __aenter__(self):
        return self._ws

    async def __aexit__(self, *exc):
        return False


def patch_ws(monkeypatch, **kwargs) -> list[FakeWebSocket]:
    """Make websockets.connect hand out a fresh FakeWebSocket each time.

    Returns the list of sockets handed out, so a test can count connections —
    which is how "the cache was used" and "the latch stopped asking" are
    asserted.
    """
    sockets: list[FakeWebSocket] = []

    def connect(url, **_):
        ws = FakeWebSocket(**kwargs)
        sockets.append(ws)
        return _FakeConnect(ws)

    monkeypatch.setattr(ha_client.websockets, "connect", connect)
    return sockets


def registry_responder(labels=None, entries=None):
    """Answer the two registry list commands, refuse anything else."""
    def respond(msg: dict) -> dict:
        if msg["type"] == "config/label_registry/list":
            return {"success": True, "result": SAMPLE_LABELS if labels is None else labels}
        if msg["type"] == "config/entity_registry/list":
            return {"success": True, "result": SAMPLE_ENTITY_ENTRIES if entries is None else entries}
        return {"success": False, "error": {"code": "unknown_command", "message": msg["type"]}}
    return respond


def refusing_responder(code: str, message: str = "Unauthorized"):
    def respond(msg: dict) -> dict:
        return {"success": False, "error": {"code": code, "message": message}}
    return respond


# ---------------------------------------------------------------------------
# ws_command
# ---------------------------------------------------------------------------

async def test_ws_command_authenticates_then_sends_the_command(monkeypatch):
    sockets = patch_ws(monkeypatch, responder=registry_responder())

    result = await ha_client.ws_command({"type": "config/label_registry/list"})

    assert result == SAMPLE_LABELS
    assert len(sockets) == 1
    auth, command = sockets[0].sent
    assert auth == {"type": "auth", "access_token": "test-token"}
    assert command == {"id": 1, "type": "config/label_registry/list"}


async def test_ws_command_raises_on_auth_failure(monkeypatch):
    patch_ws(monkeypatch, auth_ok=False, responder=registry_responder())

    with pytest.raises(ha_client.WSCommandError) as exc_info:
        await ha_client.ws_command({"type": "config/label_registry/list"})

    assert exc_info.value.code == ha_client.WS_CODE_AUTH_FAILED


async def test_ws_command_raises_on_unsuccessful_result(monkeypatch):
    patch_ws(monkeypatch, responder=refusing_responder("unauthorized"))

    with pytest.raises(ha_client.WSCommandError) as exc_info:
        await ha_client.ws_command({"type": "config/entity_registry/list"})

    assert exc_info.value.code == "unauthorized"
    assert "Unauthorized" in str(exc_info.value)


async def test_ws_command_raises_on_timeout(monkeypatch):
    monkeypatch.setattr(ha_client, "WS_COMMAND_TIMEOUT", 0.05)
    patch_ws(monkeypatch, hang=True, responder=registry_responder())

    with pytest.raises(ha_client.WSCommandError) as exc_info:
        await ha_client.ws_command({"type": "config/label_registry/list"})

    assert exc_info.value.code == ha_client.WS_CODE_TIMEOUT


async def test_ws_command_raises_when_connection_drops_mid_request(monkeypatch):
    patch_ws(monkeypatch, close_before_result=True, responder=registry_responder())

    with pytest.raises(ha_client.WSCommandError) as exc_info:
        await ha_client.ws_command({"type": "config/label_registry/list"})

    assert exc_info.value.code == ha_client.WS_CODE_CONNECTION


async def test_ws_command_raises_when_the_connection_cannot_be_opened(monkeypatch):
    def connect(url, **_):
        raise OSError("Connection refused")

    monkeypatch.setattr(ha_client.websockets, "connect", connect)

    with pytest.raises(ha_client.WSCommandError) as exc_info:
        await ha_client.ws_command({"type": "config/label_registry/list"})

    assert exc_info.value.code == ha_client.WS_CODE_CONNECTION


# ---------------------------------------------------------------------------
# get_label_registry
# ---------------------------------------------------------------------------

async def test_get_label_registry_parses_both_registries(monkeypatch):
    patch_ws(monkeypatch, responder=registry_responder())

    registry = await ha_client.get_label_registry()

    # Catalogue is sorted by display name, trimmed to what a picker renders.
    assert registry["labels"] == [
        {"label_id": "downstairs", "name": "Downstairs", "color": None, "icon": None},
        {"label_id": "guest_safe", "name": "Guest safe", "color": "green", "icon": "mdi:account"},
    ]
    # Only labelled entities are carried, so the payload tracks label use.
    assert registry["entity_labels"] == {
        "light.kitchen": ["guest_safe", "downstairs"],
        "switch.patio": ["guest_safe"],
    }


async def test_get_label_registry_returns_none_on_refusal(monkeypatch):
    patch_ws(monkeypatch, responder=refusing_responder("unauthorized"))

    assert await ha_client.get_label_registry() is None


async def test_get_label_registry_returns_none_on_timeout(monkeypatch, caplog):
    monkeypatch.setattr(ha_client, "WS_COMMAND_TIMEOUT", 0.05)
    patch_ws(monkeypatch, hang=True, responder=registry_responder())

    with caplog.at_level(logging.INFO, logger="app.ha_client"):
        assert await ha_client.get_label_registry() is None

    # A timeout is transient — warn, don't latch, so the next open retries.
    assert [r for r in caplog.records if r.levelno == logging.WARNING]
    assert ha_client._registry_denied_at is None


async def test_get_label_registry_returns_none_on_auth_failure(monkeypatch):
    patch_ws(monkeypatch, auth_ok=False, responder=registry_responder())

    assert await ha_client.get_label_registry() is None


async def test_get_label_registry_survives_a_malformed_reply(monkeypatch):
    patch_ws(monkeypatch, responder=registry_responder(labels=[{"nope": 1}]))

    assert await ha_client.get_label_registry() is None


async def test_permission_refusal_is_explained_once_then_latched(monkeypatch, caplog):
    sockets = patch_ws(monkeypatch, responder=refusing_responder("unauthorized"))

    with caplog.at_level(logging.INFO, logger="app.ha_client"):
        for _ in range(3):
            assert await ha_client.get_label_registry() is None

    # Asked once, then skipped — no per-request connection, no per-request log.
    assert len(sockets) == 1
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "label filter" in errors[0].getMessage()
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


async def test_unknown_command_latches_too(monkeypatch, caplog):
    """A core too old to have the label registry is just as permanent."""
    sockets = patch_ws(monkeypatch, responder=refusing_responder("unknown_command", "Unknown command."))

    with caplog.at_level(logging.INFO, logger="app.ha_client"):
        for _ in range(2):
            assert await ha_client.get_label_registry() is None

    assert len(sockets) == 1
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1


async def test_latched_registry_reads_recover_without_a_restart(monkeypatch, caplog):
    replies = {"refuse": True}

    def respond(msg):
        if replies["refuse"]:
            return {"success": False, "error": {"code": "unauthorized", "message": "Unauthorized"}}
        return registry_responder()(msg)

    patch_ws(monkeypatch, responder=respond)

    assert await ha_client.get_label_registry() is None
    assert ha_client._registry_denied_at is not None

    # Admin fixes the token's permissions; the next re-probe picks it up.
    ha_client._registry_denied_at -= ha_client.REGISTRY_DENIED_RETRY_SECONDS + 1
    replies["refuse"] = False
    with caplog.at_level(logging.INFO, logger="app.ha_client"):
        registry = await ha_client.get_label_registry()

    assert registry is not None
    assert ha_client._registry_denied_at is None
    assert any("answering" in r.getMessage() for r in caplog.records)


async def test_registry_is_cached_within_the_ttl_and_refreshed_after(monkeypatch):
    sockets = patch_ws(monkeypatch, responder=registry_responder())

    first = await ha_client.get_label_registry()
    second = await ha_client.get_label_registry()

    # Two commands, one connection each — the second call touched nothing.
    assert len(sockets) == 2
    assert second == first

    ha_client._registry_cache_ts -= ha_client.REGISTRY_CACHE_TTL + 1
    await ha_client.get_label_registry()
    assert len(sockets) == 4


async def test_failed_reads_are_not_cached(monkeypatch):
    """Only successes go in the cache, so a blip does not stick for the TTL."""
    monkeypatch.setattr(ha_client, "WS_COMMAND_TIMEOUT", 0.05)
    patch_ws(monkeypatch, hang=True, responder=registry_responder())
    assert await ha_client.get_label_registry() is None
    assert ha_client._registry_cache is None

    patch_ws(monkeypatch, responder=registry_responder())
    assert await ha_client.get_label_registry() is not None


# ---------------------------------------------------------------------------
# The guest SSE fan-out is not on this path
# ---------------------------------------------------------------------------

async def test_registry_read_does_not_disturb_the_sse_fan_out(monkeypatch, test_db):
    """A registry read opens its own socket; subscriptions keep delivering.

    The rest of the SSE contract is covered by the existing guest tests — this
    only pins the thing the short-lived-connection design was chosen to protect.
    """
    import time

    from app import database as db

    token = await db.create_token(
        label="Registry SSE", slug="registry-sse",
        entity_ids=["light.kitchen"], expires_at=int(time.time()) + 3600,
        ip_allowlist=None,
    )
    queue = await ha_client.subscribe(token["id"])
    try:
        patch_ws(monkeypatch, responder=registry_responder())
        assert await ha_client.get_label_registry() is not None

        await ha_client._fan_out("light.kitchen", {"entity_id": "light.kitchen", "state": "on"})
        event = queue.get_nowait()
        assert event["type"] == "state_change"
        assert event["entity_id"] == "light.kitchen"
    finally:
        await ha_client.unsubscribe(token["id"], queue)


# ---------------------------------------------------------------------------
# GET /admin/ha/entities
# ---------------------------------------------------------------------------

STATES = [
    {"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen Light"}},
    {"entity_id": "switch.patio", "state": "off", "attributes": {}},
    {"entity_id": "script.dangerous", "state": "off", "attributes": {}},
]


async def test_entities_without_include_labels_is_unchanged(client, admin_session, mock_ha_client):
    """The bare list the dashboard already fetches, and no registry read."""
    mock_ha_client["get_states"].return_value = STATES

    resp = await client.get("/admin/ha/entities", cookies=admin_session)

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert [e["entity_id"] for e in data] == ["light.kitchen", "switch.patio"]
    assert "labels" not in data[0]
    mock_ha_client["get_label_registry"].assert_not_called()


async def test_entities_with_labels_returns_both_halves_in_one_round_trip(
    client, admin_session, mock_ha_client
):
    mock_ha_client["get_states"].return_value = STATES
    mock_ha_client["get_label_registry"].return_value = {
        "labels": [{"label_id": "guest_safe", "name": "Guest safe", "color": "green", "icon": None}],
        "entity_labels": {"light.kitchen": ["guest_safe"]},
    }

    resp = await client.get("/admin/ha/entities?include_labels=true", cookies=admin_session)

    assert resp.status_code == 200
    data = resp.json()
    assert data["labels_available"] is True
    assert data["labels"] == [
        {"label_id": "guest_safe", "name": "Guest safe", "color": "green", "icon": None}
    ]
    by_id = {e["entity_id"]: e for e in data["entities"]}
    assert by_id["light.kitchen"]["labels"] == ["guest_safe"]
    assert by_id["switch.patio"]["labels"] == []
    # Same filtering as before — labels do not smuggle in unsupported domains.
    assert "script.dangerous" not in by_id


async def test_entities_with_labels_degrades_when_labels_cannot_be_read(
    client, admin_session, mock_ha_client
):
    """The picker still gets its entities; it just hides the filter."""
    mock_ha_client["get_states"].return_value = STATES
    mock_ha_client["get_label_registry"].return_value = None

    resp = await client.get("/admin/ha/entities?include_labels=true", cookies=admin_session)

    assert resp.status_code == 200
    data = resp.json()
    assert data["labels_available"] is False
    assert data["labels"] == []
    assert [e["entity_id"] for e in data["entities"]] == ["light.kitchen", "switch.patio"]
    assert all(e["labels"] == [] for e in data["entities"])


async def test_entities_with_labels_survives_a_raising_registry_read(
    client, admin_session, mock_ha_client
):
    mock_ha_client["get_states"].return_value = STATES
    mock_ha_client["get_label_registry"].side_effect = RuntimeError("boom")

    resp = await client.get("/admin/ha/entities?include_labels=true", cookies=admin_session)

    assert resp.status_code == 200
    assert resp.json()["labels_available"] is False


async def test_entities_with_labels_still_502s_when_ha_is_unreachable(
    client, admin_session, mock_ha_client
):
    mock_ha_client["get_states"].side_effect = Exception("Connection refused")

    resp = await client.get("/admin/ha/entities?include_labels=true", cookies=admin_session)

    assert resp.status_code == 502


async def test_entities_with_labels_requires_admin(client, mock_ha_client):
    resp = await client.get("/admin/ha/entities?include_labels=true")
    assert resp.status_code == 401
