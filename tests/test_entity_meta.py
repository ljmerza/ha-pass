"""Per-token entity presentation overrides (display name + display options).

show_brightness is opt-in: absent means the guest gets on/off only.

These are presentation-only: nothing here may widen what a token can reach, and
the overrides must survive an entity add/remove, which rebuilds the row set.
"""
import time

import pytest
import pytest_asyncio

from app import database as db


@pytest_asyncio.fixture
async def meta_token(test_db):
    now = int(time.time())
    return await db.create_token(
        label="Meta Token",
        slug="meta-token",
        entity_ids=["light.living_room", "light.kitchen"],
        expires_at=now + 3600,
        ip_allowlist=None,
        entity_meta={"light.living_room": {"display_name": "Reading Lamp", "options": {"show_brightness": True}}},
    )


@pytest.mark.asyncio
async def test_create_persists_display_name_and_options(meta_token):
    meta = await db.get_token_entity_meta(meta_token["id"])
    assert meta["light.living_room"]["display_name"] == "Reading Lamp"
    assert meta["light.living_room"]["options"] == {"show_brightness": True}
    assert meta["light.kitchen"]["display_name"] is None
    assert meta["light.kitchen"]["options"] == {}


@pytest.mark.asyncio
async def test_overrides_survive_entity_list_edit(meta_token, mock_ha_client):
    """update_token_entities rebuilds every row - names must not be collateral."""
    await db.update_token_entities(
        meta_token["id"], ["light.living_room", "light.kitchen", "switch.fan"]
    )
    meta = await db.get_token_entity_meta(meta_token["id"])
    assert meta["light.living_room"]["display_name"] == "Reading Lamp"
    assert meta["light.living_room"]["options"] == {"show_brightness": True}


@pytest.mark.asyncio
async def test_removing_an_entity_drops_its_override(meta_token, mock_ha_client):
    await db.update_token_entities(meta_token["id"], ["light.kitchen"])
    meta = await db.get_token_entity_meta(meta_token["id"])
    assert "light.living_room" not in meta


@pytest.mark.asyncio
async def test_guest_state_exposes_overrides(client, meta_token, mock_ha_client):
    resp = await client.get(f"/g/{meta_token['slug']}/state")
    assert resp.status_code == 200
    body = resp.json()
    assert body["entity_meta"]["light.living_room"]["display_name"] == "Reading Lamp"


@pytest.mark.asyncio
async def test_admin_can_set_and_clear_one_entity(client, admin_session, meta_token, mock_ha_client):
    r = await client.patch(
        f"/admin/tokens/{meta_token['id']}/entity-meta",
        json={"entity_id": "light.kitchen", "display_name": "  Counter Light  "},
        cookies=admin_session,
    )
    assert r.status_code == 200
    assert r.json()["display_name"] == "Counter Light"   # trimmed

    r = await client.patch(
        f"/admin/tokens/{meta_token['id']}/entity-meta",
        json={"entity_id": "light.kitchen", "display_name": "   "},
        cookies=admin_session,
    )
    assert r.status_code == 200
    assert r.json()["display_name"] is None              # blank clears it
    meta = await db.get_token_entity_meta(meta_token["id"])
    assert meta["light.kitchen"]["display_name"] is None


@pytest.mark.asyncio
async def test_cannot_set_meta_for_entity_not_on_token(client, admin_session, meta_token, mock_ha_client):
    r = await client.patch(
        f"/admin/tokens/{meta_token['id']}/entity-meta",
        json={"entity_id": "lock.front_door", "display_name": "Sneaky"},
        cookies=admin_session,
    )
    assert r.status_code == 404
    # and it must not have been added to the allowlist as a side effect
    assert "lock.front_door" not in await db.get_token_entities(meta_token["id"])


@pytest.mark.asyncio
async def test_unknown_option_keys_are_dropped(client, admin_session, meta_token, mock_ha_client):
    r = await client.patch(
        f"/admin/tokens/{meta_token['id']}/entity-meta",
        json={
            "entity_id": "light.kitchen",
            "options": {"show_brightness": True, "allow_everything": True},
        },
        cookies=admin_session,
    )
    assert r.status_code == 200
    assert r.json()["options"] == {"show_brightness": True}


@pytest.mark.asyncio
async def test_display_name_is_length_capped(client, admin_session, meta_token, mock_ha_client):
    r = await client.patch(
        f"/admin/tokens/{meta_token['id']}/entity-meta",
        json={"entity_id": "light.kitchen", "display_name": "x" * 200},
        cookies=admin_session,
    )
    assert r.status_code == 422   # pydantic max_length rejects before it reaches the DB


@pytest.mark.asyncio
async def test_meta_does_not_widen_the_allowlist(client, meta_token, mock_ha_client):
    """A renamed entity is still just an entity; nothing else becomes reachable."""
    resp = await client.post(
        f"/g/{meta_token['slug']}/command",
        json={"entity_id": "lock.front_door", "service": "lock.unlock"},
    )
    assert resp.status_code == 403
