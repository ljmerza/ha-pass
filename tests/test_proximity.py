"""Per-entity proximity gate: the command path, the zone lookup, and the page
that only reaches for the geolocation API when it has a reason to.

Same shape as test_guest_security.py — real routing, real DB, real rate limiter,
real distance maths; only ha_client is mocked. Two properties carry most of the
weight here:

  * the gate is per-entity, so an ungated entity on the same token must never
    need, wait on, or be refused for a location; and
  * it is enforced on the server, so a hand-rolled POST that omits the location,
    replays an old one, or simply asserts "I'm at home" gets nowhere.
"""
import math
import time

import pytest
import pytest_asyncio
from unittest.mock import patch

from app import database as db
from app import guest_pin
from app import proximity
from app.routers.guest import PROXIMITY_FAILURE_LIMITS

from tests.conftest import HOME_ZONE

GATED = "input_button.open_door"
UNGATED = "light.living_room"
PIN = "7391"


def _point_north(metres: float) -> dict[str, float]:
    """A point `metres` due north of the mocked home zone.

    Computed, not measured: along a meridian the haversine distance reduces
    exactly to R x dphi, so the boundary cases land on the radius precisely
    instead of near it.
    """
    return {
        "latitude": HOME_ZONE["latitude"] + math.degrees(metres / proximity.EARTH_RADIUS_METERS),
        "longitude": HOME_ZONE["longitude"],
    }


def _fix(metres: float = 0.0, age_seconds: float = 0.0) -> dict:
    """A location payload `metres` from home, stamped `age_seconds` ago."""
    return {
        **_point_north(metres),
        "timestamp": int((time.time() - age_seconds) * 1000),
    }


async def _make_token(slug: str, gated: bool = True, pin: str | None = None):
    """A token carrying one gated entity and one ungated one on the same link."""
    return await db.create_token(
        label=f"Token {slug}",
        slug=slug,
        entity_ids=[GATED, UNGATED],
        expires_at=int(time.time()) + 3600,
        ip_allowlist=None,
        entity_meta={GATED: {"require_proximity": True}} if gated else None,
        pin_hash=await guest_pin.hash_pin(pin) if pin else None,
    )


async def _press(client, slug: str, entity_id: str = GATED, location: dict | None = None):
    body = {"entity_id": entity_id, "service": "press" if entity_id == GATED else "turn_on"}
    if location is not None:
        body["location"] = location
    return await client.post(f"/g/{slug}/command", json=body)


@pytest_asyncio.fixture
async def gated_token(test_db):
    return await _make_token("gated")


@pytest_asyncio.fixture
async def plain_token(test_db):
    return await _make_token("plain", gated=False)


# ---------------------------------------------------------------------------
# A token with nothing gated is untouched
# ---------------------------------------------------------------------------

async def test_ungated_token_command_needs_no_location(client, plain_token, mock_ha_client):
    resp = await _press(client, plain_token["slug"], UNGATED)
    assert resp.status_code == 200
    mock_ha_client["call_service"].assert_called_once()
    # The zone is never read, so a gate-free token does not depend on HA having
    # a usable zone.home at all.
    mock_ha_client["get_home_zone"].assert_not_called()


async def test_ungated_token_page_never_mentions_geolocation(client, plain_token, mock_ha_client):
    """The strong form of the requirement: not "does not prompt", but "cannot".

    The geolocation block is emitted by the template only when the token has a
    gated entity, so for an ordinary link the API is not named anywhere in the
    page and no code path exists that could reach it.
    """
    resp = await client.get(f"/g/{plain_token['slug']}")
    assert resp.status_code == 200
    page = resp.text.lower()
    for banned in ("geolocation", "getcurrentposition", "watchposition", "coords"):
        assert banned not in page, f"ungated guest page references {banned}"


async def test_ungated_token_state_payload_carries_no_gate(client, plain_token, mock_ha_client):
    resp = await client.get(f"/g/{plain_token['slug']}/state")
    assert resp.status_code == 200
    meta = resp.json()["entity_meta"]
    assert meta, "expected per-entity meta for both entities"
    assert all(m["require_proximity"] is False for m in meta.values())


async def test_gated_token_page_includes_the_geolocation_block(client, gated_token, mock_ha_client):
    resp = await client.get(f"/g/{gated_token['slug']}")
    assert resp.status_code == 200
    assert "navigator.geolocation" in resp.text
    assert "getCurrentPosition" in resp.text


async def test_gated_token_state_payload_flags_only_the_gated_entity(
    client, gated_token, mock_ha_client
):
    resp = await client.get(f"/g/{gated_token['slug']}/state")
    meta = resp.json()["entity_meta"]
    assert meta[GATED]["require_proximity"] is True
    assert meta[UNGATED]["require_proximity"] is False


# ---------------------------------------------------------------------------
# Server-side enforcement
# ---------------------------------------------------------------------------

async def test_gated_command_without_location_is_refused(client, gated_token, mock_ha_client):
    resp = await _press(client, gated_token["slug"])
    assert resp.status_code == 400
    mock_ha_client["call_service"].assert_not_called()


async def test_gated_command_outside_the_zone_is_refused(client, gated_token, mock_ha_client):
    resp = await _press(client, gated_token["slug"], location=_fix(metres=5000))
    assert resp.status_code == 403
    mock_ha_client["call_service"].assert_not_called()


async def test_gated_command_inside_the_zone_is_allowed(client, gated_token, mock_ha_client):
    resp = await _press(client, gated_token["slug"], location=_fix(metres=20))
    assert resp.status_code == 200
    args = mock_ha_client["call_service"].call_args[0]
    assert args[0] == "input_button"
    assert args[1] == "press"
    # The location is a gate, not a service argument — it must not reach HA.
    assert "location" not in args[2]
    assert "latitude" not in args[2]


async def test_gated_command_exactly_on_the_radius_is_allowed(client, gated_token, mock_ha_client):
    """The boundary is inclusive — see proximity.is_within_zone."""
    location = _fix(metres=HOME_ZONE["radius"])
    assert proximity.haversine_meters(
        location["latitude"], location["longitude"],
        HOME_ZONE["latitude"], HOME_ZONE["longitude"],
    ) == pytest.approx(HOME_ZONE["radius"], abs=1e-6)

    resp = await _press(client, gated_token["slug"], location=location)
    assert resp.status_code == 200


async def test_gated_command_just_past_the_radius_is_refused(client, gated_token, mock_ha_client):
    resp = await _press(client, gated_token["slug"], location=_fix(metres=HOME_ZONE["radius"] + 1))
    assert resp.status_code == 403
    mock_ha_client["call_service"].assert_not_called()


async def test_replayed_fix_goes_stale(client, gated_token, mock_ha_client):
    """A set of coordinates captured once must not keep working.

    Soft, and deliberately described that way: the timestamp comes from the same
    browser as the coordinates, so forging it is no harder. It stops a request
    body being saved and re-sent later, not a guest who edits both.
    """
    stale = _fix(metres=10, age_seconds=proximity.MAX_FIX_AGE_SECONDS + 5)
    resp = await _press(client, gated_token["slug"], location=stale)
    assert resp.status_code == 400
    mock_ha_client["call_service"].assert_not_called()


async def test_fix_from_a_slightly_slow_clock_still_passes(client, gated_token, mock_ha_client):
    """The freshness window has to survive an ordinary slow fix, not just an
    instant one — 20s covers a cold high-accuracy GPS lock plus the round trip."""
    resp = await _press(client, gated_token["slug"], location=_fix(metres=10, age_seconds=20))
    assert resp.status_code == 200


async def test_fix_stamped_far_in_the_future_is_refused(client, gated_token, mock_ha_client):
    ahead = _fix(metres=10, age_seconds=-(proximity.MAX_FIX_SKEW_SECONDS + 60))
    resp = await _press(client, gated_token["slug"], location=ahead)
    assert resp.status_code == 400


async def test_client_asserting_it_is_home_without_coordinates_is_refused(
    client, gated_token, mock_ha_client
):
    """There is no "in zone" boolean to send, and inventing one changes nothing.

    Both shapes are covered: a location object carrying only a claim (rejected
    as malformed), and a claim smuggled alongside the command (ignored, leaving
    the command with no location at all).
    """
    claim_as_location = await client.post(
        f"/g/{gated_token['slug']}/command",
        json={"entity_id": GATED, "service": "press", "location": {"in_zone": True}},
    )
    assert claim_as_location.status_code == 422

    claim_alongside = await client.post(
        f"/g/{gated_token['slug']}/command",
        json={"entity_id": GATED, "service": "press", "in_zone": True, "at_home": True},
    )
    assert claim_alongside.status_code == 400
    mock_ha_client["call_service"].assert_not_called()


async def test_coordinates_outside_the_earth_are_refused(client, gated_token, mock_ha_client):
    resp = await _press(
        client, gated_token["slug"],
        location={"latitude": 91.0, "longitude": 0.0, "timestamp": int(time.time() * 1000)},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Per-entity, not per-token
# ---------------------------------------------------------------------------

async def test_ungated_entity_on_a_gated_token_still_works_with_no_location(
    client, gated_token, mock_ha_client
):
    """The whole point of doing this per-entity: gating the door button must not
    make the living-room lamp ask anyone where they are."""
    resp = await _press(client, gated_token["slug"], UNGATED)
    assert resp.status_code == 200
    mock_ha_client["call_service"].assert_called_once()
    mock_ha_client["get_home_zone"].assert_not_called()


async def test_ungated_entity_unaffected_when_the_gate_cannot_be_checked(
    client, gated_token, mock_ha_client
):
    mock_ha_client["get_home_zone"].return_value = None

    refused = await _press(client, gated_token["slug"], location=_fix(metres=10))
    assert refused.status_code == 503

    allowed = await _press(client, gated_token["slug"], UNGATED)
    assert allowed.status_code == 200


# ---------------------------------------------------------------------------
# zone.home — fail closed
# ---------------------------------------------------------------------------

async def test_unreadable_zone_home_fails_closed(client, gated_token, mock_ha_client):
    """A gate that opens when it cannot verify is not a gate.

    The cost of erring this way is that a gated control stops working while HA
    is unreachable or zone.home is misconfigured — which the admin notices. The
    cost of erring the other way is that the same conditions silently open it.
    """
    mock_ha_client["get_home_zone"].return_value = None
    resp = await _press(client, gated_token["slug"], location=_fix(metres=10))
    assert resp.status_code == 503
    mock_ha_client["call_service"].assert_not_called()


@pytest.fixture
def clear_zone_cache():
    from app import ha_client
    ha_client._home_zone = None
    ha_client._home_zone_ts = 0.0
    yield
    ha_client._home_zone = None
    ha_client._home_zone_ts = 0.0


def _stub_ha_response(payload):
    """Minimal stand-in for the httpx client ha_client holds."""
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    class _Client:
        async def get(self, url):
            assert url == "/api/states/zone.home"
            return _Resp()

    return _Client()


@pytest.mark.parametrize("payload", [
    {},                                                          # no attributes at all
    {"attributes": {}},                                          # zone.home with nothing on it
    {"attributes": {"latitude": 40.0, "longitude": -75.0}},      # no radius
    {"attributes": {"latitude": "over there", "longitude": -75.0, "radius": 100}},
    {"attributes": {"latitude": None, "longitude": None, "radius": None}},
])
async def test_malformed_zone_home_reads_as_unverifiable(clear_zone_cache, payload):
    from app import ha_client
    with patch.object(ha_client, "_client", _stub_ha_response(payload)):
        assert await ha_client.get_home_zone() is None


async def test_well_formed_zone_home_is_parsed_and_cached(clear_zone_cache):
    from app import ha_client
    payload = {"attributes": {"latitude": "40.5", "longitude": -75.5, "radius": 250}}
    with patch.object(ha_client, "_client", _stub_ha_response(payload)):
        assert await ha_client.get_home_zone() == {
            "latitude": 40.5, "longitude": -75.5, "radius": 250.0,
        }
    # Second read comes from the cache, so it survives the client going away.
    with patch.object(ha_client, "_client", None):
        assert (await ha_client.get_home_zone())["radius"] == 250.0


async def test_unreachable_ha_reads_as_unverifiable(clear_zone_cache):
    from app import ha_client

    class _Boom:
        async def get(self, url):
            raise RuntimeError("HA is down")

    with patch.object(ha_client, "_client", _Boom()):
        assert await ha_client.get_home_zone() is None


# ---------------------------------------------------------------------------
# Distance and freshness maths
# ---------------------------------------------------------------------------

def test_haversine_matches_a_known_meridian_distance():
    """One degree of latitude on a sphere of this radius is R x 1 radian/180."""
    expected = proximity.EARTH_RADIUS_METERS * math.radians(1)
    assert proximity.haversine_meters(0.0, 0.0, 1.0, 0.0) == pytest.approx(expected, rel=1e-9)


def test_zone_membership_is_inclusive_at_the_radius():
    edge = _point_north(HOME_ZONE["radius"])
    assert proximity.is_within_zone(edge["latitude"], edge["longitude"], HOME_ZONE)
    past = _point_north(HOME_ZONE["radius"] + 0.5)
    assert not proximity.is_within_zone(past["latitude"], past["longitude"], HOME_ZONE)


def test_freshness_window_bounds():
    now = time.time()
    fresh = int((now - 1) * 1000)
    stale = int((now - proximity.MAX_FIX_AGE_SECONDS - 1) * 1000)
    ahead = int((now + proximity.MAX_FIX_SKEW_SECONDS + 1) * 1000)
    assert proximity.fix_is_fresh(fresh, now)
    assert not proximity.fix_is_fresh(stale, now)
    assert not proximity.fix_is_fresh(ahead, now)


# ---------------------------------------------------------------------------
# Composition with the PIN gate
# ---------------------------------------------------------------------------

async def test_pin_is_checked_before_any_location(client, test_db, mock_ha_client):
    """The PIN gate lives in _validate_token, so it runs first.

    A valid location must not stand in for the PIN, and the refusal must not
    leak that the entity happens to be gated.
    """
    token = await _make_token("pinned", pin=PIN)
    resp = await _press(client, token["slug"], location=_fix(metres=10))
    assert resp.status_code == 401
    mock_ha_client["call_service"].assert_not_called()
    mock_ha_client["get_home_zone"].assert_not_called()


async def test_pin_alone_does_not_open_a_gated_entity(client, test_db, mock_ha_client):
    token = await _make_token("pinned2", pin=PIN)
    unlock = await client.post(f"/g/{token['slug']}/pin", data={"pin": PIN})
    assert unlock.status_code == 303

    no_location = await _press(client, token["slug"])
    assert no_location.status_code == 400

    away = await _press(client, token["slug"], location=_fix(metres=5000))
    assert away.status_code == 403

    at_home = await _press(client, token["slug"], location=_fix(metres=10))
    assert at_home.status_code == 200


async def test_pin_token_ungated_entity_needs_no_location(client, test_db, mock_ha_client):
    token = await _make_token("pinned3", pin=PIN)
    assert (await client.post(f"/g/{token['slug']}/pin", data={"pin": PIN})).status_code == 303
    resp = await _press(client, token["slug"], UNGATED)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

async def test_repeated_refusals_exhaust_a_budget_of_their_own(
    client, gated_token, mock_ha_client
):
    """Refusals are counted so the gate is not a free oracle for the house's
    coordinates, and so a guest who is genuinely away stops retrying."""
    per_minute = PROXIMITY_FAILURE_LIMITS[0][1]
    for _ in range(per_minute):
        resp = await _press(client, gated_token["slug"], location=_fix(metres=5000))
        assert resp.status_code == 403

    spent = await _press(client, gated_token["slug"], location=_fix(metres=5000))
    assert spent.status_code == 429


async def test_a_spent_refusal_budget_does_not_block_ungated_entities(
    client, gated_token, mock_ha_client
):
    for _ in range(PROXIMITY_FAILURE_LIMITS[0][1] + 1):
        await _press(client, gated_token["slug"], location=_fix(metres=5000))

    resp = await _press(client, gated_token["slug"], UNGATED)
    assert resp.status_code == 200


async def test_being_at_home_never_touches_the_refusal_budget(
    client, gated_token, mock_ha_client
):
    for _ in range(PROXIMITY_FAILURE_LIMITS[0][1] + 3):
        resp = await _press(client, gated_token["slug"], location=_fix(metres=10))
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Storage and the admin API
# ---------------------------------------------------------------------------

async def test_gate_is_stored_in_its_own_column_not_the_options_blob(test_db):
    token = await _make_token("stored")
    conn = await db.get_db()
    async with conn.execute(
        "SELECT entity_id, options, require_proximity FROM token_entities "
        "WHERE token_id = ?", (token["id"],)
    ) as cur:
        rows = {r["entity_id"]: (r["options"], r["require_proximity"]) for r in await cur.fetchall()}

    assert rows[GATED][1] == 1
    assert rows[UNGATED][1] == 0
    # The presentation blob stays free of it — that split is the reason for the
    # column, since nothing in the command path reads `options`.
    assert rows[GATED][0] is None
    assert await db.get_proximity_entity_ids(token["id"]) == {GATED}


async def test_admin_can_set_the_gate_per_entity(client, admin_session, mock_ha_client):
    created = await client.post(
        "/admin/tokens",
        json={
            "label": "Cleaner",
            "entity_ids": [GATED, UNGATED],
            "expires_in_seconds": 3600,
            "entity_meta": {GATED: {"require_proximity": True}},
        },
        cookies=admin_session,
    )
    assert created.status_code == 201
    token_id = created.json()["id"]

    fetched = await client.get(f"/admin/tokens/{token_id}", cookies=admin_session)
    meta = fetched.json()["entity_meta"]
    assert meta[GATED]["require_proximity"] is True
    assert meta[UNGATED]["require_proximity"] is False


async def test_admin_can_toggle_the_gate_on_an_existing_entity(
    client, admin_session, gated_token, mock_ha_client
):
    resp = await client.patch(
        f"/admin/tokens/{gated_token['id']}/entity-meta",
        json={"entity_id": UNGATED, "require_proximity": True},
        cookies=admin_session,
    )
    assert resp.status_code == 200
    assert resp.json()["require_proximity"] is True
    assert await db.get_proximity_entity_ids(gated_token["id"]) == {GATED, UNGATED}

    cleared = await client.patch(
        f"/admin/tokens/{gated_token['id']}/entity-meta",
        json={"entity_id": GATED},
        cookies=admin_session,
    )
    assert cleared.status_code == 200
    assert cleared.json()["require_proximity"] is False
    assert await db.get_proximity_entity_ids(gated_token["id"]) == {UNGATED}


async def test_editing_the_entity_list_preserves_the_gate(
    client, admin_session, gated_token, mock_ha_client
):
    """update_token_entities rebuilds the whole row set, so an unrelated edit
    must not quietly drop a gate the admin set earlier."""
    resp = await client.patch(
        f"/admin/tokens/{gated_token['id']}/entities",
        json={"entity_ids": [GATED, UNGATED, "switch.porch"]},
        cookies=admin_session,
    )
    assert resp.status_code == 200
    assert await db.get_proximity_entity_ids(gated_token["id"]) == {GATED}
