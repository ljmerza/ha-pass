"""Named entity templates: save a picker selection, replay it later — issue #6.

A template stores entity IDs and nothing else. The per-entity overrides a token
carries (display_name, the options blob, require_proximity) stay with the token
they were set on, so the tests below check both halves of that: what a template
round-trips, and what it deliberately does not.

Real routing, real DB; only ha_client is mocked.
"""
import time

import pytest

from app import database as db
from app.models import TEMPLATE_NAME_MAX


async def _create(client, admin_session, name, entity_ids):
    return await client.post(
        "/admin/templates",
        json={"name": name, "entity_ids": entity_ids},
        cookies=admin_session,
    )


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------

async def test_create_list_and_delete_round_trip(client, admin_session, test_db, mock_ha_client):
    resp = await _create(client, admin_session, "Ground floor",
                         ["light.living_room", "switch.porch"])
    assert resp.status_code == 201
    created = resp.json()
    assert created["name"] == "Ground floor"
    assert created["entity_ids"] == ["light.living_room", "switch.porch"]
    assert created["created_at"] <= int(time.time())
    assert created["id"]

    listed = await client.get("/admin/templates", cookies=admin_session)
    assert listed.status_code == 200
    assert listed.json() == [created]

    gone = await client.delete(f"/admin/templates/{created['id']}", cookies=admin_session)
    assert gone.status_code == 200
    assert (await client.get("/admin/templates", cookies=admin_session)).json() == []


async def test_a_template_loads_into_a_new_token(client, admin_session, test_db, mock_ha_client):
    """The whole point: the saved IDs create a token without re-picking them."""
    tpl = (await _create(client, admin_session, "Guest set",
                         ["light.living_room", "camera.hall"])).json()

    resp = await client.post(
        "/admin/tokens",
        json={
            "label": "From template",
            "entity_ids": tpl["entity_ids"],
            "expires_in_seconds": 3600,
        },
        cookies=admin_session,
    )
    assert resp.status_code == 201
    assert sorted(resp.json()["entity_ids"]) == ["camera.hall", "light.living_room"]


async def test_templates_are_listed_by_name(client, admin_session, test_db, mock_ha_client):
    for name in ("zebra", "Apple", "mango"):
        assert (await _create(client, admin_session, name, ["light.a"])).status_code == 201
    names = [t["name"] for t in (await client.get("/admin/templates", cookies=admin_session)).json()]
    assert names == ["Apple", "mango", "zebra"]


async def test_duplicate_entity_ids_are_collapsed(client, admin_session, test_db, mock_ha_client):
    resp = await _create(client, admin_session, "Dupes",
                         ["light.a", "light.b", "light.a"])
    assert resp.json()["entity_ids"] == ["light.a", "light.b"]


# ---------------------------------------------------------------------------
# What a template deliberately does not store
# ---------------------------------------------------------------------------

async def test_a_template_stores_only_entity_ids(client, admin_session, test_db, mock_ha_client):
    """Presentation and the proximity gate belong to a token, not to a reusable
    set — a saved template must not be able to carry an access control into a
    later link."""
    resp = await _create(client, admin_session, "Ids only", ["light.a"])
    assert set(resp.json()) == {"id", "name", "entity_ids", "created_at"}

    row = await db.get_entity_template(resp.json()["id"])
    assert set(row) == {"id", "name", "entity_ids", "created_at"}


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

async def test_a_name_over_the_cap_is_rejected(client, admin_session, test_db, mock_ha_client):
    resp = await _create(client, admin_session, "x" * (TEMPLATE_NAME_MAX + 1), ["light.a"])
    assert resp.status_code == 422
    assert (await client.get("/admin/templates", cookies=admin_session)).json() == []

    ok = await _create(client, admin_session, "x" * TEMPLATE_NAME_MAX, ["light.a"])
    assert ok.status_code == 201


@pytest.mark.parametrize("name", ["", "   "])
async def test_a_blank_name_is_rejected(client, admin_session, test_db, mock_ha_client, name):
    resp = await _create(client, admin_session, name, ["light.a"])
    assert resp.status_code == 422


async def test_a_name_is_trimmed(client, admin_session, test_db, mock_ha_client):
    resp = await _create(client, admin_session, "  Ground floor  ", ["light.a"])
    assert resp.json()["name"] == "Ground floor"


async def test_a_duplicate_name_is_refused_case_insensitively(
    client, admin_session, test_db, mock_ha_client
):
    assert (await _create(client, admin_session, "Ground Floor", ["light.a"])).status_code == 201
    dupe = await _create(client, admin_session, "ground floor", ["light.b"])
    assert dupe.status_code == 409
    assert len((await client.get("/admin/templates", cookies=admin_session)).json()) == 1


async def test_a_name_is_escaped_when_the_dashboard_renders_it(
    client, admin_session, test_db, mock_ha_client
):
    """Names are free text from the admin and are written into the picker with
    innerHTML, so the chip goes through esc() like every other label."""
    resp = await _create(client, admin_session, "<img src=x onerror=alert(1)>", ["light.a"])
    assert resp.status_code == 201
    # The API stores it verbatim; escaping is the renderer's job, and the chip
    # markup runs the name through esc().
    assert resp.json()["name"] == "<img src=x onerror=alert(1)>"
    page = await client.get("/admin/dashboard", cookies=admin_session)
    assert "${esc(t.name)}" in page.text


async def test_an_empty_entity_list_is_rejected(client, admin_session, test_db, mock_ha_client):
    resp = await _create(client, admin_session, "Nothing", [])
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Access control and errors
# ---------------------------------------------------------------------------

async def test_templates_are_admin_only(client, test_db, mock_ha_client):
    assert (await client.get("/admin/templates")).status_code == 401
    assert (await client.post(
        "/admin/templates", json={"name": "x", "entity_ids": ["light.a"]}
    )).status_code == 401
    assert (await client.delete("/admin/templates/whatever")).status_code == 401
    assert await db.list_entity_templates() == []


async def test_deleting_an_unknown_template_is_404(client, admin_session, test_db, mock_ha_client):
    resp = await client.delete("/admin/templates/nope", cookies=admin_session)
    assert resp.status_code == 404


async def test_deleting_a_template_leaves_tokens_alone(
    client, admin_session, test_db, mock_ha_client
):
    tpl = (await _create(client, admin_session, "Temp", ["light.living_room"])).json()
    token = await client.post(
        "/admin/tokens",
        json={"label": "T", "entity_ids": tpl["entity_ids"], "expires_in_seconds": 3600},
        cookies=admin_session,
    )
    token_id = token.json()["id"]

    assert (await client.delete(f"/admin/templates/{tpl['id']}",
                                cookies=admin_session)).status_code == 200
    assert await db.get_token_entities(token_id) == ["light.living_room"]
