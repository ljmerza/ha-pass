"""Slug rotation: a fresh link for an existing token — upstream issue #6.

The point of the feature is reuse: the same entities, overrides, expiry, PIN
and history handed to a different guest under a link the previous one does not
have. So most of what is asserted here is what *didn't* change.

Real routing, real DB, real bcrypt; only ha_client is mocked.
"""
import time

import pytest
import pytest_asyncio

from app import database as db
from app import guest_pin

PIN = "4821"


@pytest_asyncio.fixture
async def rich_token(test_db):
    """A token carrying every attribute rotation has to preserve."""
    return await db.create_token(
        label="Airbnb Guest",
        slug="original-slug",
        entity_ids=["light.living_room", "camera.hall", "input_button.door"],
        expires_at=int(time.time()) + 3600,
        ip_allowlist=None,
        entity_meta={
            "light.living_room": {
                "display_name": "Sophia's Room",
                "options": {"show_brightness": True},
                "require_proximity": False,
            },
            "input_button.door": {
                "display_name": None,
                "options": None,
                "require_proximity": True,
            },
        },
        pin_hash=await guest_pin.hash_pin(PIN),
    )


async def _rotate(client, admin_session, token_id):
    resp = await client.post(f"/admin/tokens/{token_id}/rotate-slug", cookies=admin_session)
    assert resp.status_code == 200
    return resp.json()


# ---------------------------------------------------------------------------
# The slug itself
# ---------------------------------------------------------------------------

async def test_rotation_mints_a_new_slug_of_the_same_shape(
    client, admin_session, rich_token, mock_ha_client
):
    body = await _rotate(client, admin_session, rich_token["id"])
    assert body["slug"] != "original-slug"
    # Same generator as creation: 16 random bytes, hex.
    assert len(body["slug"]) == 32
    assert all(c in "0123456789abcdef" for c in body["slug"])
    assert body["id"] == rich_token["id"]


async def test_rotation_never_echoes_the_old_slug(
    client, admin_session, rich_token, mock_ha_client, caplog
):
    """The retired slug is still a credential. It must not survive in a
    response body or a log line."""
    with caplog.at_level("DEBUG"):
        resp = await client.post(
            f"/admin/tokens/{rich_token['id']}/rotate-slug", cookies=admin_session
        )
    assert "original-slug" not in resp.text
    assert "original-slug" not in caplog.text


async def test_two_rotations_do_not_repeat(client, admin_session, rich_token, mock_ha_client):
    first = (await _rotate(client, admin_session, rich_token["id"]))["slug"]
    second = (await _rotate(client, admin_session, rich_token["id"]))["slug"]
    assert first != second


# ---------------------------------------------------------------------------
# The old link
# ---------------------------------------------------------------------------

async def test_the_old_link_is_gone_and_the_new_one_works(
    client, admin_session, rich_token, mock_ha_client
):
    body = await _rotate(client, admin_session, rich_token["id"])

    # Same 410 an unknown slug has always produced — rotation must not become
    # a way to tell "used to exist" from "never existed".
    old = await client.get("/g/original-slug")
    assert old.status_code == 410
    assert (await client.get("/g/original-slug/state")).status_code == 410

    # The new one is live, behind the PIN it already had.
    new = await client.get(f"/g/{body['slug']}")
    assert new.status_code == 200
    assert "Enter PIN" in new.text


async def test_rotation_hangs_up_streams_on_the_old_link(
    client, admin_session, rich_token, mock_ha_client
):
    """An SSE stream is validated once, at connect, and then runs until the
    token expires — so rotation has to close it explicitly."""
    await _rotate(client, admin_session, rich_token["id"])
    mock_ha_client["broadcast_token_expired"].assert_awaited_once_with(rich_token["id"])


# ---------------------------------------------------------------------------
# Everything that must survive
# ---------------------------------------------------------------------------

async def test_entities_and_overrides_survive(
    client, admin_session, rich_token, mock_ha_client
):
    before_meta = await db.get_token_entity_meta(rich_token["id"])
    await _rotate(client, admin_session, rich_token["id"])

    assert sorted(await db.get_token_entities(rich_token["id"])) == [
        "camera.hall", "input_button.door", "light.living_room",
    ]
    assert await db.get_token_entity_meta(rich_token["id"]) == before_meta
    # The proximity gate is an access control, not presentation — check it
    # through the function the command path actually reads.
    assert await db.get_proximity_entity_ids(rich_token["id"]) == {"input_button.door"}


async def test_expiry_label_and_revocation_state_survive(
    client, admin_session, rich_token, mock_ha_client
):
    body = await _rotate(client, admin_session, rich_token["id"])
    assert body["expires_at"] == rich_token["expires_at"]
    assert body["label"] == "Airbnb Guest"
    assert body["revoked"] is False


async def test_the_pin_survives_and_still_unlocks_the_new_link(
    client, admin_session, rich_token, mock_ha_client
):
    body = await _rotate(client, admin_session, rich_token["id"])
    assert body["has_pin"] is True

    row = await db.get_token_by_id(rich_token["id"])
    assert row["pin_hash"] == rich_token["pin_hash"]

    unlock = await client.post(f"/g/{body['slug']}/pin", data={"pin": PIN})
    assert unlock.status_code == 303
    assert (await client.get(f"/g/{body['slug']}/state")).status_code == 200


async def test_an_existing_pin_session_does_not_carry_over(
    client, admin_session, rich_token, mock_ha_client
):
    """The PIN cookie is scoped Path=/g/<old-slug>, so the browser never offers
    it to the new link, and the old link is 410 in any case. The new guest is
    asked for the PIN — which is the point of rotating.

    The signature itself is still valid (it is keyed on the token's bcrypt hash
    and the token id, neither of which rotation touches), so this asserts the
    scoping rather than a signature failure: even replayed by hand, the cookie
    only ever reaches a slug that is gone.
    """
    unlock = await client.post("/g/original-slug/pin", data={"pin": PIN})
    assert unlock.status_code == 303
    raw = unlock.headers["set-cookie"]
    assert "path=/g/original-slug" in raw.lower()
    session = raw.split(";")[0].split("=", 1)[1]
    assert (await client.get("/g/original-slug/state")).status_code == 200

    body = await _rotate(client, admin_session, rich_token["id"])

    # Replayed by hand at the new slug the old session still verifies: it
    # commits to a token id and an expiry, and rotation changes neither. That
    # is not a hole — reaching this point means already holding the new slug,
    # which is the whole credential. What matters is that no browser can do it,
    # because the cookie was set with Path=/g/original-slug and is never sent
    # to the new link. A guest opening the rotated link sees the PIN screen.
    resp = await client.get(
        f"/g/{body['slug']}/state",
        headers={"Cookie": f"{guest_pin.SESSION_COOKIE}={session}"},
    )
    assert resp.status_code == 200

    page = await client.get(f"/g/{body['slug']}")
    assert "Enter PIN" in page.text


async def test_access_history_survives(client, admin_session, rich_token, mock_ha_client):
    """The access log is keyed on token id, not slug — verified, not assumed."""
    await db.log_access(rich_token["id"], "page_load", ip_address="10.0.0.1")
    await db.log_access(rich_token["id"], "command", entity_id="light.living_room",
                        service="light.turn_on")
    await db.touch_token(rich_token["id"])
    before = await db.get_token_by_id(rich_token["id"])

    await _rotate(client, admin_session, rich_token["id"])

    rows = await db.list_access_logs(limit=50)
    mine = [r for r in rows if r["token_label"] == "Airbnb Guest"]
    assert len(mine) == 2
    assert {r["event_type"] for r in mine} == {"page_load", "command"}

    after = await db.get_token_by_id(rich_token["id"])
    assert after["last_accessed"] == before["last_accessed"]

    activity = await client.get("/admin/activity", cookies=admin_session)
    assert len([a for a in activity.json() if a["token_label"] == "Airbnb Guest"]) == 2


# ---------------------------------------------------------------------------
# Access control and errors
# ---------------------------------------------------------------------------

async def test_rotation_requires_admin(client, rich_token, mock_ha_client):
    resp = await client.post(f"/admin/tokens/{rich_token['id']}/rotate-slug")
    assert resp.status_code == 401
    # Nothing moved.
    assert (await db.get_token_by_id(rich_token["id"]))["slug"] == "original-slug"


async def test_rotation_of_an_unknown_token_is_404(client, admin_session, test_db, mock_ha_client):
    resp = await client.post(
        "/admin/tokens/00000000-0000-0000-0000-000000000000/rotate-slug",
        cookies=admin_session,
    )
    assert resp.status_code == 404


async def test_rotation_is_a_post_not_a_delete(client, admin_session, rich_token, mock_ha_client):
    """Consistent with revoke, which was deliberately moved off DELETE."""
    resp = await client.request(
        "DELETE", f"/admin/tokens/{rich_token['id']}/rotate-slug", cookies=admin_session
    )
    assert resp.status_code == 405
