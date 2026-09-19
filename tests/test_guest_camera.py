"""Camera proxy security tests.

Same shape as test_guest_security.py: real routing, real DB, real validation and
rate limiting; only ha_client is mocked. The point of these is that a guest can
never reach a camera outside their allowlist, and can never *control* a camera.
"""
import time

import pytest
import pytest_asyncio

from app import database as db


@pytest_asyncio.fixture
async def camera_token(test_db):
    """Token holding one camera plus one light — so cross-entity leaks are visible."""
    now = int(time.time())
    return await db.create_token(
        label="Camera Token",
        slug="cam-token",
        entity_ids=["camera.back_bedroom_camera", "light.living_room"],
        expires_at=now + 3600,
        ip_allowlist=None,
    )


@pytest.mark.asyncio
async def test_allowlisted_camera_returns_image(client, camera_token, mock_ha_client):
    resp = await client.get(f"/g/{camera_token['slug']}/camera/camera.back_bedroom_camera")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.headers["cache-control"] == "no-store"
    mock_ha_client["camera_snapshot"].assert_awaited_once_with("camera.back_bedroom_camera")


@pytest.mark.asyncio
async def test_camera_not_in_allowlist_is_forbidden(client, camera_token, mock_ha_client):
    resp = await client.get(f"/g/{camera_token['slug']}/camera/camera.master_bedroom_camera")
    assert resp.status_code == 403
    mock_ha_client["camera_snapshot"].assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_not_in_allowlist_is_forbidden(client, camera_token):
    resp = await client.get(f"/g/{camera_token['slug']}/camera/camera.driveway_camera/stream")
    assert resp.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    "light.living_room",            # allowlisted, but not a camera
    "camera.Back_Bedroom",          # uppercase
    "camera.back-bedroom",          # hyphen
    "camera",                       # no object_id
    "camera.a/../../api/states",    # traversal into the HA REST API
])
async def test_malformed_entity_is_rejected(client, camera_token, mock_ha_client, bad):
    resp = await client.get(f"/g/{camera_token['slug']}/camera/{bad}")
    assert resp.status_code in (403, 404, 422)
    mock_ha_client["camera_snapshot"].assert_not_awaited()


@pytest.mark.asyncio
async def test_camera_is_read_only(client, camera_token, mock_ha_client):
    """camera is in READ_ONLY_DOMAINS, so every camera service must be refused."""
    for service in ("turn_on", "turn_off", "snapshot", "record"):
        resp = await client.post(
            f"/g/{camera_token['slug']}/command",
            json={"entity_id": "camera.back_bedroom_camera", "service": f"camera.{service}"},
        )
        assert resp.status_code == 403, service
    mock_ha_client["call_service"].assert_not_awaited()


@pytest.mark.asyncio
async def test_revoked_token_cannot_reach_camera(client, camera_token, mock_ha_client):
    await db.revoke_token(camera_token["id"])
    resp = await client.get(f"/g/{camera_token['slug']}/camera/camera.back_bedroom_camera")
    assert resp.status_code == 410
    mock_ha_client["camera_snapshot"].assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_passes_through_upstream_content_type(client, camera_token):
    resp = await client.get(f"/g/{camera_token['slug']}/camera/camera.back_bedroom_camera/stream")
    assert resp.status_code == 200
    assert "multipart/x-mixed-replace" in resp.headers["content-type"]
    assert "boundary=--frameboundary" in resp.headers["content-type"]
