"""Tests for ingress detection and ingress-based auth bypass.

These cover the new ingress feature: header spoofing prevention,
admin auth bypass for HA sidebar, login disabled in add-on mode,
and logout sentinel safety.
"""
from unittest.mock import patch

import app.ingress
from app import database as db


# ---------------------------------------------------------------------------
# Security: header spoofing prevention
# ---------------------------------------------------------------------------

def test_ingress_header_ignored_without_supervisor_token():
    """Without SUPERVISOR_TOKEN, X-Ingress-Path is untrusted — blocks spoofing."""
    from unittest.mock import MagicMock
    req = MagicMock()
    req.headers = {"X-Ingress-Path": "/api/hassio_ingress/spoofed"}
    with patch.object(app.ingress, "_SUPERVISOR_TOKEN", None):
        assert app.ingress.get_ingress_path(req) == ""


# ---------------------------------------------------------------------------
# Integration: ingress auth bypass
# ---------------------------------------------------------------------------

async def test_ingress_bypass_grants_admin_access(client, mock_ha_client, test_db):
    """Ingress requests skip session auth — admin endpoints accessible without cookie."""
    with patch("app.auth.is_ingress_request", return_value=True):
        resp = await client.get("/admin/tokens")
    assert resp.status_code == 200


async def test_login_returns_403_when_no_password(client, mock_ha_client, test_db):
    """In add-on mode (empty password), login endpoint returns 403."""
    from app.config import settings
    with patch.object(settings, "admin_password", ""):
        resp = await client.post(
            "/admin/login",
            json={"username": "testadmin", "password": "anything"},
        )
    assert resp.status_code == 403
    assert "Login disabled" in resp.json()["detail"]


async def test_ingress_logout_does_not_delete_real_sessions(client, mock_ha_client, test_db):
    """Ingress logout returns ok without accidentally wiping a real session."""
    session_id = await db.create_admin_session(ttl_seconds=86400)
    with patch("app.auth.is_ingress_request", return_value=True):
        resp = await client.post("/admin/logout")
    assert resp.status_code == 200
    row = await db.get_admin_session(session_id)
    assert row is not None


# ---------------------------------------------------------------------------
# Guest PWA: API calls must carry the ingress prefix
# ---------------------------------------------------------------------------

INGRESS_PREFIX = "/api/hassio_ingress/abc123"


async def test_guest_pwa_api_calls_carry_the_ingress_prefix(
    client, mock_ha_client, sample_token
):
    """Under ingress the page is served from a prefixed path, so the state,
    stream, command and camera URLs the JS builds have to be prefixed too.

    They were root-absolute, which 404s behind the Supervisor proxy — the static
    asset tags used base_path but the fetch/EventSource calls did not.
    """
    with patch.object(app.ingress, "_SUPERVISOR_TOKEN", "fake-supervisor-token"):
        resp = await client.get(
            "/g/test-token", headers={"X-Ingress-Path": INGRESS_PREFIX}
        )

    assert resp.status_code == 200
    body = resp.text
    assert f'const BASE = "{INGRESS_PREFIX}"' in body
    # No guest API path may start at the root.
    assert "`/g/${SLUG}" not in body
    for suffix in ("state", "stream", "command", "camera"):
        assert f"${{BASE}}/g/${{SLUG}}/{suffix}" in body


async def test_guest_pwa_api_calls_have_no_prefix_in_standalone_mode(
    client, mock_ha_client, sample_token
):
    """Without a supervisor token BASE is empty, so the URLs stay root-absolute."""
    with patch.object(app.ingress, "_SUPERVISOR_TOKEN", None):
        resp = await client.get(
            "/g/test-token", headers={"X-Ingress-Path": INGRESS_PREFIX}
        )

    assert resp.status_code == 200
    assert 'const BASE = ""' in resp.text
