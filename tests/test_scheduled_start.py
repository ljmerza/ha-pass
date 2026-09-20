"""Scheduled start ("valid from") — upstream issue #23.

A token can be created and its link shared days before it works. Until the
start time the guest gets their real card list, greyed out, behind a countdown
— and nothing else. The properties under test:

  * a token with no starts_at behaves exactly as it always did;
  * the gate is server-side, on every guest route that reads state or acts,
    both camera endpoints included, and it composes with the PIN and the
    per-entity proximity gate in that order;
  * a pending guest never receives real Home Assistant state — not from /state,
    not over the stream, not as a camera frame — which is asserted by the HA
    client never being called at all; and
  * the expiry is anchored to the start, so a link minted a week early is still
    worth its full validity when the guest arrives.

Real routing, real DB, real bcrypt, real rate limiter; only ha_client is mocked.
"""
import asyncio
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio

from app import database as db
from app import guest_pin
from app import ha_client
from app.models import NEVER_EXPIRES_SECONDS
from app.routers.guest import PROXIMITY_FAILURE_LIMITS, _event_generator

from tests.conftest import HOME_ZONE

PIN = "5173"
GATED = "input_button.door"
ENTITIES = ["light.living_room", "camera.hall", GATED]

# Every guest route that reads state or performs an action, as
# (method, path suffix, json body). /stream is deliberately absent — it is the
# one route a pending token may hold open, and it has its own tests below.
GATED_ENDPOINTS = [
    ("GET", "/state", None),
    ("GET", "/camera/camera.hall", None),
    ("GET", "/camera/camera.hall/stream", None),
    ("POST", "/command", {"entity_id": "light.living_room", "service": "turn_on"}),
]

# Everything on the mock that would touch Home Assistant for real. A pending
# guest must provoke none of them.
HA_CALLS = ("get_states", "call_service", "camera_snapshot", "get_home_zone",
            "fire_event", "logbook_log")


async def _make_token(slug: str, starts_in: int | None = 3600,
                      expires_in: int = 86400, pin: str | None = None,
                      gated: bool = False):
    now = int(time.time())
    starts_at = now + starts_in if starts_in is not None else None
    return await db.create_token(
        label=f"Token {slug}",
        slug=slug,
        entity_ids=ENTITIES,
        expires_at=(starts_at or now) + expires_in,
        ip_allowlist=None,
        entity_meta={GATED: {"require_proximity": True}} if gated else None,
        pin_hash=await guest_pin.hash_pin(pin) if pin else None,
        starts_at=starts_at,
    )


def _assert_no_ha_calls(mock_ha_client):
    for name in HA_CALLS:
        assert not mock_ha_client[name].called, f"pending guest reached ha_client.{name}"


async def _call(client, slug: str, method: str, suffix: str, body):
    if method == "GET":
        return await client.get(f"/g/{slug}{suffix}")
    return await client.post(f"/g/{slug}{suffix}", json=body)


@pytest_asyncio.fixture
async def pending_token(test_db):
    return await _make_token("pending")


@pytest_asyncio.fixture
async def open_token(test_db):
    return await _make_token("open", starts_in=None)


# ---------------------------------------------------------------------------
# Regression: nothing changes for a token that was never scheduled
# ---------------------------------------------------------------------------

async def test_unscheduled_token_is_untouched(client, open_token, mock_ha_client):
    """The default path, end to end: no starts_at, no preview, no refusals."""
    assert open_token["starts_at"] is None

    page = await client.get(f"/g/{open_token['slug']}")
    assert page.status_code == 200
    assert "Not active yet" not in page.text
    assert 'id="conn-badge"' in page.text

    for method, suffix, body in GATED_ENDPOINTS:
        resp = await _call(client, open_token["slug"], method, suffix, body)
        assert resp.status_code == 200, f"{method} {suffix} regressed"


async def test_creating_without_starts_at_anchors_to_now(client, admin_session, mock_ha_client):
    before = int(time.time())
    resp = await client.post(
        "/admin/tokens",
        json={"label": "Plain", "entity_ids": ["light.living_room"],
              "expires_in_seconds": 3600},
        cookies=admin_session,
    )
    assert resp.status_code == 201
    token = resp.json()
    assert token["starts_at"] is None
    assert before + 3600 <= token["expires_at"] <= int(time.time()) + 3600


async def test_unscheduled_page_load_is_still_logged(client, open_token, mock_ha_client):
    await client.get(f"/g/{open_token['slug']}")
    row = await db.get_token_by_id(open_token["id"])
    assert row["last_accessed"] is not None


# ---------------------------------------------------------------------------
# The gate: every guest route, before the start time
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,suffix,body", GATED_ENDPOINTS)
async def test_every_guest_route_is_refused_while_pending(
    client, pending_token, mock_ha_client, method, suffix, body
):
    resp = await _call(client, pending_token["slug"], method, suffix, body)
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["error"] == "This link is not active yet"
    assert detail["starts_at"] == pending_token["starts_at"]


async def test_refusals_never_reach_home_assistant(client, pending_token, mock_ha_client):
    """The strong form: not "the response is empty" but "HA was never asked"."""
    await client.get(f"/g/{pending_token['slug']}")
    for method, suffix, body in GATED_ENDPOINTS:
        await _call(client, pending_token["slug"], method, suffix, body)
    _assert_no_ha_calls(mock_ha_client)


async def test_command_is_refused_server_side(client, pending_token, mock_ha_client):
    """A hand-rolled POST gets nowhere — the UI hiding the controls is not the
    control. No service call is forwarded and nothing is written to the log."""
    resp = await client.post(
        f"/g/{pending_token['slug']}/command",
        json={"entity_id": "light.living_room", "service": "light.turn_on"},
    )
    assert resp.status_code == 403
    mock_ha_client["call_service"].assert_not_awaited()
    logs = await db.list_access_logs(limit=50)
    assert not [r for r in logs if r["event_type"] == "command"]


async def test_pending_visit_is_not_an_access(client, pending_token, mock_ha_client):
    """Opening the link early is not using it: nothing is touched or logged,
    the same way an unanswered PIN prompt is not an access."""
    resp = await client.get(f"/g/{pending_token['slug']}")
    assert resp.status_code == 200
    row = await db.get_token_by_id(pending_token["id"])
    assert row["last_accessed"] is None
    assert not await db.list_access_logs(limit=50)


async def test_revoked_beats_pending(client, admin_session, pending_token, mock_ha_client):
    """A dead token is dead whatever its schedule said — 410, not 403."""
    await db.revoke_token(pending_token["id"])
    resp = await client.get(f"/g/{pending_token['slug']}/state")
    assert resp.status_code == 410


async def test_ip_allowlist_beats_pending(client, test_db, mock_ha_client):
    row = await db.create_token(
        label="Fenced", slug="fenced", entity_ids=["light.living_room"],
        expires_at=int(time.time()) + 86400, ip_allowlist=["10.0.0.0/8"],
        starts_at=int(time.time()) + 3600,
    )
    resp = await client.get(f"/g/{row['slug']}/state")
    assert resp.status_code == 403
    # The allowlist refusal, not the schedule one: someone off-network learns
    # nothing about when the link opens.
    assert resp.json()["detail"] == "IP not allowed"


# ---------------------------------------------------------------------------
# The preview page
# ---------------------------------------------------------------------------

async def test_preview_carries_entities_and_names_but_no_state(
    client, test_db, mock_ha_client
):
    await db.create_token(
        label="Named", slug="named", entity_ids=["light.living_room"],
        expires_at=int(time.time()) + 86400, ip_allowlist=None,
        entity_meta={"light.living_room": {"display_name": "Sophia Bedside"}},
        starts_at=int(time.time()) + 3600,
    )
    resp = await client.get("/g/named")
    assert resp.status_code == 200
    # The entity id gives the domain, and so the icon, grouping and order; the
    # override gives the name. Between them the preview is the real card list.
    assert "light.living_room" in resp.text
    assert "Sophia Bedside" in resp.text
    assert "Not active yet" in resp.text
    _assert_no_ha_calls(mock_ha_client)


async def test_preview_hides_the_live_badge(client, pending_token, mock_ha_client):
    """Nothing live is connected, so the page must not claim otherwise."""
    resp = await client.get(f"/g/{pending_token['slug']}")
    assert 'id="conn-badge"' not in resp.text


async def test_preview_never_asks_for_a_location(client, test_db, mock_ha_client):
    """A pending page can command nothing, so it must not be able to prompt —
    the same strong form the ungated-token test in test_proximity.py asserts."""
    await _make_token("geo-pending", gated=True)
    resp = await client.get("/g/geo-pending")
    page = resp.text.lower()
    for banned in ("geolocation", "getcurrentposition", "watchposition", "coords"):
        assert banned not in page, f"pending guest page references {banned}"


async def test_preview_is_not_a_full_screen_blocker(client, pending_token, mock_ha_client):
    """The issue asked for a dimmed preview, not an overlay — the cards are
    rendered and the banner is docked, not covering them."""
    resp = await client.get(f"/g/{pending_token['slug']}")
    assert 'id="pending-banner"' in resp.text
    assert "fixed bottom-0" in resp.text
    assert "PREVIEW_ENTITY_IDS" in resp.text


# ---------------------------------------------------------------------------
# Expiry anchoring
# ---------------------------------------------------------------------------

async def test_expiry_is_anchored_to_the_start(client, admin_session, mock_ha_client):
    """A 3-day token created a week early is three days of guest access, not a
    token that died four days before the guest arrived."""
    starts_at = int(time.time()) + 7 * 86400
    resp = await client.post(
        "/admin/tokens",
        json={"label": "Airbnb", "entity_ids": ["light.living_room"],
              "expires_in_seconds": 3 * 86400, "starts_at": starts_at},
        cookies=admin_session,
    )
    assert resp.status_code == 201
    assert resp.json()["expires_at"] == starts_at + 3 * 86400


async def test_never_expires_is_not_shifted_by_a_start(client, admin_session, mock_ha_client):
    """The sentinel is an absolute date, not a duration — adding a start time
    to it would push it past the value every "no expiration" test compares."""
    resp = await client.post(
        "/admin/tokens",
        json={"label": "Forever", "entity_ids": ["light.living_room"],
              "expires_in_seconds": NEVER_EXPIRES_SECONDS,
              "starts_at": int(time.time()) + 86400},
        cookies=admin_session,
    )
    assert resp.status_code == 201
    assert resp.json()["expires_at"] == NEVER_EXPIRES_SECONDS


async def test_a_past_start_is_folded_to_none(client, admin_session, mock_ha_client):
    """"Valid from" yesterday means "now", which is what None means — so the
    expiry anchors to now and the token is active immediately."""
    before = int(time.time())
    resp = await client.post(
        "/admin/tokens",
        json={"label": "Stale", "entity_ids": ["light.living_room"],
              "expires_in_seconds": 3600, "starts_at": before - 600},
        cookies=admin_session,
    )
    assert resp.status_code == 201
    token = resp.json()
    assert token["starts_at"] is None
    assert token["expires_at"] <= int(time.time()) + 3600

    state = await client.get(f"/g/{token['slug']}/state")
    assert state.status_code == 200


async def test_extending_a_pending_token_anchors_to_the_start(
    client, admin_session, pending_token, mock_ha_client
):
    """Extending a link that has not opened yet buys the guest that much
    access, measured from their check-in — not from the admin's click."""
    resp = await client.patch(
        f"/admin/tokens/{pending_token['id']}/expiry",
        json={"expires_in_seconds": 2 * 86400},
        cookies=admin_session,
    )
    assert resp.status_code == 200
    assert resp.json()["expires_at"] == pending_token["starts_at"] + 2 * 86400


async def test_extending_an_active_token_still_anchors_to_now(
    client, admin_session, open_token, mock_ha_client
):
    before = int(time.time())
    resp = await client.patch(
        f"/admin/tokens/{open_token['id']}/expiry",
        json={"expires_in_seconds": 7200},
        cookies=admin_session,
    )
    assert before + 7200 <= resp.json()["expires_at"] <= int(time.time()) + 7200


async def test_a_start_beyond_the_sentinel_is_rejected(client, admin_session, mock_ha_client):
    resp = await client.post(
        "/admin/tokens",
        json={"label": "Absurd", "entity_ids": ["light.living_room"],
              "expires_in_seconds": 3600, "starts_at": NEVER_EXPIRES_SECONDS + 1},
        cookies=admin_session,
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------

async def test_the_token_works_once_the_start_time_passes(client, test_db, mock_ha_client):
    """No admin action and no reissue — the clock alone opens the link."""
    token = await _make_token("boundary", starts_in=1)
    assert (await client.get(f"/g/{token['slug']}/state")).status_code == 403
    await asyncio.sleep(1.2)
    assert (await client.get(f"/g/{token['slug']}/state")).status_code == 200


# ---------------------------------------------------------------------------
# Activate Now
# ---------------------------------------------------------------------------

async def test_activate_now_opens_the_link(client, admin_session, pending_token, mock_ha_client):
    resp = await client.post(
        f"/admin/tokens/{pending_token['id']}/activate", cookies=admin_session
    )
    assert resp.status_code == 200
    assert resp.json()["starts_at"] is None
    assert (await client.get(f"/g/{pending_token['slug']}/state")).status_code == 200


async def test_activate_now_leaves_the_expiry_alone(
    client, admin_session, pending_token, mock_ha_client
):
    """The end was anchored to the start the admin picked, and that end is a
    calendar fact. Starting early lengthens the window; it does not slide it."""
    await client.post(f"/admin/tokens/{pending_token['id']}/activate", cookies=admin_session)
    row = await db.get_token_by_id(pending_token["id"])
    assert row["expires_at"] == pending_token["expires_at"]


async def test_activate_now_pushes_to_connected_guests(
    client, admin_session, pending_token, mock_ha_client
):
    await client.post(f"/admin/tokens/{pending_token['id']}/activate", cookies=admin_session)
    mock_ha_client["broadcast_token_activated"].assert_awaited_once_with(pending_token["id"])


async def test_activate_now_is_admin_only(client, pending_token, mock_ha_client):
    resp = await client.post(f"/admin/tokens/{pending_token['id']}/activate")
    assert resp.status_code == 401
    row = await db.get_token_by_id(pending_token["id"])
    assert row["starts_at"] == pending_token["starts_at"]


async def test_activate_now_rejects_an_unscheduled_token(
    client, admin_session, open_token, mock_ha_client
):
    resp = await client.post(
        f"/admin/tokens/{open_token['id']}/activate", cookies=admin_session
    )
    assert resp.status_code == 400


async def test_activate_now_rejects_a_revoked_token(
    client, admin_session, pending_token, mock_ha_client
):
    await db.revoke_token(pending_token["id"])
    resp = await client.post(
        f"/admin/tokens/{pending_token['id']}/activate", cookies=admin_session
    )
    assert resp.status_code == 400
    row = await db.get_token_by_id(pending_token["id"])
    assert row["starts_at"] == pending_token["starts_at"]


async def test_activate_now_on_an_unknown_token_is_404(client, admin_session, mock_ha_client):
    resp = await client.post("/admin/tokens/nope/activate", cookies=admin_session)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# The stream: the one route a pending token may hold open
# ---------------------------------------------------------------------------

def _fake_request():
    """Enough of a Request for _event_generator: it only asks about hangup."""
    async def _is_disconnected():
        return False
    return SimpleNamespace(is_disconnected=_is_disconnected)


async def _drain(gen, count: int, timeout: float = 2.0):
    frames = []
    for _ in range(count):
        frames.append(await asyncio.wait_for(gen.__anext__(), timeout=timeout))
    return frames


async def test_only_the_stream_opts_out_of_the_gate(test_db, pending_token):
    """The gate defaults closed in _validate_token, so a route added later is
    refused before the start time unless it says otherwise. /stream is the only
    caller that does, and this is the seam that decides it.

    Asserted here rather than over HTTP because httpx's ASGI transport buffers
    a response whole — a live SSE endpoint never returns to it.
    """
    from fastapi import HTTPException
    from app.routers.guest import _validate_token

    request = SimpleNamespace(headers={}, cookies={}, client=SimpleNamespace(host="1.2.3.4"))

    row = await _validate_token(pending_token["slug"], request, allow_pending=True)
    assert row["id"] == pending_token["id"]

    with pytest.raises(HTTPException) as exc:
        await _validate_token(pending_token["slug"], request)
    assert exc.value.status_code == 403


async def test_pending_stream_forwards_activation(test_db, pending_token):
    # No mock_ha_client here on purpose: the broadcast under test is the real
    # one, writing to the real queue this generator is reading from.
    gen = _event_generator(
        pending_token["id"], pending_token["slug"], _fake_request(),
        pending_token["starts_at"],
    )
    assert "event: connected" in await asyncio.wait_for(gen.__anext__(), timeout=2)

    task = asyncio.create_task(_drain(gen, 1))
    await asyncio.sleep(0.05)
    await ha_client.broadcast_token_activated(pending_token["id"])
    assert "event: token_activated" in (await asyncio.wait_for(task, timeout=2))[0]
    await gen.aclose()


async def test_pending_stream_drops_state_changes(test_db, pending_token):
    """No device state leaves the server while pending — the frames are never
    serialised, rather than merely ignored by the page."""
    gen = _event_generator(
        pending_token["id"], pending_token["slug"], _fake_request(),
        pending_token["starts_at"],
    )
    await asyncio.wait_for(gen.__anext__(), timeout=2)

    task = asyncio.create_task(_drain(gen, 1))
    await asyncio.sleep(0.05)
    await ha_client._fan_out("light.living_room", {"state": "on"})
    await ha_client.broadcast_token_activated(pending_token["id"])
    frame = (await asyncio.wait_for(task, timeout=2))[0]
    assert "token_activated" in frame
    assert "state_change" not in frame and "light.living_room" not in frame
    await gen.aclose()


async def test_pending_stream_activates_itself_at_the_boundary(
    test_db, mock_ha_client
):
    """The tab may be asleep and its own timer may never fire, so the server
    watches the clock too and pushes when the moment arrives."""
    token = await _make_token("stream-boundary", starts_in=1)
    gen = _event_generator(token["id"], token["slug"], _fake_request(), token["starts_at"])
    await asyncio.wait_for(gen.__anext__(), timeout=2)
    assert "event: token_activated" in await asyncio.wait_for(gen.__anext__(), timeout=3)
    await gen.aclose()


async def test_an_active_stream_is_unaffected(test_db, open_token, mock_ha_client):
    """The regression half: with no schedule the generator still relays state."""
    gen = _event_generator(open_token["id"], open_token["slug"], _fake_request(), None)
    await asyncio.wait_for(gen.__anext__(), timeout=2)

    task = asyncio.create_task(_drain(gen, 1))
    await asyncio.sleep(0.05)
    await ha_client._fan_out("light.living_room", {"state": "on"})
    assert "event: state_change" in (await asyncio.wait_for(task, timeout=2))[0]
    await gen.aclose()


# ---------------------------------------------------------------------------
# Composition with the PIN gate
# ---------------------------------------------------------------------------

async def test_a_locked_pending_token_asks_for_the_pin_first(client, test_db, mock_ha_client):
    """The preview names every entity on the link, so it is not something to
    hand to someone who has not proved the PIN. The schedule is not mentioned."""
    await _make_token("locked-pending", pin=PIN)
    resp = await client.get("/g/locked-pending")
    assert resp.status_code == 200
    assert "Enter PIN" in resp.text
    assert "Not active yet" not in resp.text
    assert "light.living_room" not in resp.text


async def test_a_locked_pending_api_answers_401_not_403(client, test_db, mock_ha_client):
    """Ordering, at the API: PIN before schedule, so an unauthenticated caller
    cannot learn that the link exists and starts on Tuesday."""
    await _make_token("locked-api", pin=PIN)
    resp = await client.get("/g/locked-api/state")
    assert resp.status_code == 401
    assert "starts_at" not in resp.text


async def test_the_preview_appears_once_the_pin_is_entered(client, test_db, mock_ha_client):
    token = await _make_token("locked-then", pin=PIN)
    unlock = await client.post("/g/locked-then/pin", data={"pin": PIN})
    assert unlock.status_code == 303

    page = await client.get("/g/locked-then")
    assert "Not active yet" in page.text

    state = await client.get("/g/locked-then/state")
    assert state.status_code == 403
    assert state.json()["detail"]["starts_at"] == token["starts_at"]
    _assert_no_ha_calls(mock_ha_client)


# ---------------------------------------------------------------------------
# Composition with the proximity gate
# ---------------------------------------------------------------------------

async def test_pending_is_decided_before_proximity(client, test_db, mock_ha_client):
    """The schedule is a token-wide gate and the proximity one is per-entity, so
    the schedule answers first: a guest standing at the door before check-in is
    told the link is not open yet, not that they are in the wrong place."""
    token = await _make_token("prox-pending", gated=True)
    resp = await client.post(
        f"/g/{token['slug']}/command",
        json={"entity_id": GATED, "service": "press",
              "location": {**HOME_ZONE, "timestamp": int(time.time() * 1000)}},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "This link is not active yet"
    # And the zone was never read: the gate that did not run cost nothing.
    mock_ha_client["get_home_zone"].assert_not_awaited()


async def test_pending_refusals_do_not_spend_the_proximity_budget(
    client, test_db, mock_ha_client
):
    """A pending refusal is not metered — it is a comparison against a row
    already in hand and reveals nothing the banner did not. It must also not
    drain the budget that protects the proximity gate, or a guest who retried
    while waiting would be rate-limited at the moment their access opened."""
    token = await _make_token("prox-budget", gated=True)
    attempts = PROXIMITY_FAILURE_LIMITS[0][1] + 3
    for _ in range(attempts):
        resp = await client.post(
            f"/g/{token['slug']}/command", json={"entity_id": GATED, "service": "press"}
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "This link is not active yet"

    from app.rate_limiter import rate_limiter
    assert f"prox:{token['id']}" not in rate_limiter._windows


# ---------------------------------------------------------------------------
# Composition with duplicate and rotate
# ---------------------------------------------------------------------------

async def test_duplicate_reads_the_schedule_back(
    client, admin_session, pending_token, mock_ha_client
):
    """The dashboard's Duplicate reads this endpoint to prefill the modal, so
    starts_at has to be on it — the modal then deliberately blanks the field,
    because a start time belongs to one booking the way a slug belongs to one
    guest."""
    resp = await client.get(f"/admin/tokens/{pending_token['id']}", cookies=admin_session)
    assert resp.json()["starts_at"] == pending_token["starts_at"]


async def test_a_copy_without_a_start_is_live_immediately(
    client, admin_session, pending_token, mock_ha_client
):
    resp = await client.post(
        "/admin/tokens",
        json={"label": "Token pending copy", "entity_ids": ENTITIES,
              "expires_in_seconds": 86400},
        cookies=admin_session,
    )
    assert resp.status_code == 201
    assert resp.json()["starts_at"] is None
    assert (await client.get(f"/g/{resp.json()['slug']}/state")).status_code == 200


async def test_rotation_keeps_the_schedule(
    client, admin_session, pending_token, mock_ha_client
):
    """Rotation hands the same configuration to a different guest. The booking
    window is part of that configuration, so it survives — only the URL moves."""
    resp = await client.post(
        f"/admin/tokens/{pending_token['id']}/rotate-slug", cookies=admin_session
    )
    assert resp.status_code == 200
    new_slug = resp.json()["slug"]
    assert resp.json()["starts_at"] == pending_token["starts_at"]
    assert new_slug != pending_token["slug"]

    assert (await client.get(f"/g/{pending_token['slug']}/state")).status_code == 410
    assert (await client.get(f"/g/{new_slug}/state")).status_code == 403


async def test_rotation_hangs_up_a_pending_stream(
    client, admin_session, pending_token, mock_ha_client
):
    """A pending stream outlives the slug it was opened on for exactly the same
    reason an active one does, so rotation still has to close it."""
    await client.post(
        f"/admin/tokens/{pending_token['id']}/rotate-slug", cookies=admin_session
    )
    mock_ha_client["broadcast_token_expired"].assert_awaited_once_with(pending_token["id"])
