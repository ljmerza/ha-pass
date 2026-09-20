"""Optional per-token PIN: the guest gate, the session that follows it, and the
admin side that sets it.

Same shape as test_guest_security.py — real routing, real DB, real bcrypt, real
rate limiter; only ha_client is mocked. The property under test throughout is
that a PIN gates *every* guest endpoint, not just the HTML page: the slug alone
must not reach state, the SSE stream, a command, or either camera endpoint.
"""
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio

from app import database as db
from app import guest_pin
from app.routers.guest import (
    PIN_ATTEMPT_LIMITS_PER_IP,
    PIN_ATTEMPT_LIMITS_PER_TOKEN,
)

PIN = "4821"
WRONG_PIN = "1234"

# Every guest endpoint that reads state or performs an action, as
# (method, path suffix, json body). Camera snapshot and stream are in here
# because they relay live frames — the fork gated neither.
GATED_ENDPOINTS = [
    ("GET", "/state", None),
    ("GET", "/stream", None),
    ("GET", "/camera/camera.hall", None),
    ("GET", "/camera/camera.hall/stream", None),
    ("POST", "/command", {"entity_id": "light.living_room", "service": "turn_on"}),
]


async def _make_token(slug: str, pin: str | None = PIN, expires_in: int = 3600):
    return await db.create_token(
        label=f"Token {slug}",
        slug=slug,
        entity_ids=["light.living_room", "camera.hall"],
        expires_at=int(time.time()) + expires_in,
        ip_allowlist=None,
        pin_hash=await guest_pin.hash_pin(pin) if pin else None,
    )


def _session_value(resp) -> str:
    """Pull the raw PIN session cookie out of a 303 response.

    Read from the header rather than the client jar so it can be replayed
    against a different slug — the cookie's Path would otherwise stop httpx
    sending it, and the point of those tests is the HMAC binding underneath.
    """
    raw = resp.headers["set-cookie"]
    assert raw.startswith(f"{guest_pin.SESSION_COOKIE}=")
    return raw.split(";")[0].split("=", 1)[1]


def _cookie_header(value: str) -> dict:
    return {"Cookie": f"{guest_pin.SESSION_COOKIE}={value}"}


async def _unlock(client, slug: str, pin: str = PIN) -> str:
    resp = await client.post(f"/g/{slug}/pin", data={"pin": pin})
    assert resp.status_code == 303
    return _session_value(resp)


@pytest_asyncio.fixture
async def pin_token(test_db):
    return await _make_token("pin-token")


@pytest_asyncio.fixture
async def open_token(test_db):
    return await _make_token("open-token", pin=None)


# ---------------------------------------------------------------------------
# Regression: a token with no PIN behaves exactly as before
# ---------------------------------------------------------------------------

async def test_token_without_pin_is_unaffected(client, open_token, mock_ha_client):
    """No PIN is the default. Nothing about those requests may change."""
    page = await client.get("/g/open-token")
    assert page.status_code == 200
    assert "Enter PIN" not in page.text

    assert (await client.get("/g/open-token/state")).status_code == 200
    assert (await client.get("/g/open-token/manifest.json")).status_code == 200
    assert (await client.get("/g/open-token/camera/camera.hall")).status_code == 200
    assert (
        await client.get("/g/open-token/camera/camera.hall/stream")
    ).status_code == 200

    cmd = await client.post(
        "/g/open-token/command",
        json={"entity_id": "light.living_room", "service": "turn_on"},
    )
    assert cmd.status_code == 200
    mock_ha_client["call_service"].assert_called_once()


async def test_sse_gate_matches_the_other_endpoints(test_db, open_token, pin_token):
    """The SSE route cannot be driven end-to-end here — httpx's ASGI transport
    buffers the whole body, and the stream never ends — so its gate is checked
    at _validate_token, which is the only PIN logic on that path and the one
    every other guest endpoint shares.
    """
    from fastapi import HTTPException

    from app.routers.guest import _validate_token

    stub = SimpleNamespace(cookies={})
    assert await _validate_token("open-token", stub)

    with pytest.raises(HTTPException) as exc:
        await _validate_token("pin-token", stub)
    assert exc.value.status_code == 401

    value, _ = guest_pin.issue_session(
        pin_token["id"], (await db.get_token_by_id(pin_token["id"]))["pin_hash"],
        pin_token["expires_at"],
    )
    unlocked = SimpleNamespace(cookies={guest_pin.SESSION_COOKIE: value})
    assert await _validate_token("pin-token", unlocked)


async def test_page_load_logging_unchanged_without_pin(client, open_token, mock_ha_client):
    await client.get("/g/open-token")
    row = await db.get_token_by_id(open_token["id"])
    assert row["last_accessed"] is not None


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,suffix,body", GATED_ENDPOINTS)
async def test_every_guest_endpoint_is_gated(
    client, pin_token, mock_ha_client, method, suffix, body
):
    """No cookie, PIN set: identical 401 everywhere, and no data leaves."""
    resp = await client.request(method, f"/g/pin-token{suffix}", json=body)
    assert resp.status_code == 401
    assert resp.json() == {"detail": "PIN required"}
    assert resp.headers["content-type"].startswith("application/json")

    mock_ha_client["call_service"].assert_not_called()
    mock_ha_client["camera_snapshot"].assert_not_called()


async def test_gated_page_serves_pin_entry_not_the_app(client, pin_token, mock_ha_client):
    resp = await client.get("/g/pin-token")
    assert resp.status_code == 200
    assert "Enter PIN" in resp.text
    # The guest shell and its entity data must not be rendered behind the prompt.
    assert "cards-container" not in resp.text


async def test_gated_page_is_not_an_access(client, pin_token, mock_ha_client):
    """An unanswered prompt is not a visit — nothing touched, nothing logged."""
    await client.get("/g/pin-token")

    row = await db.get_token_by_id(pin_token["id"])
    assert row["last_accessed"] is None

    conn = await db.get_db()
    async with conn.execute(
        "SELECT COUNT(*) AS cnt FROM access_log WHERE token_id = ?", (pin_token["id"],)
    ) as cur:
        assert (await cur.fetchone())["cnt"] == 0

    mock_ha_client["fire_event"].assert_not_called()
    mock_ha_client["logbook_log"].assert_not_called()


async def test_manifest_stays_reachable_while_locked(client, pin_token, mock_ha_client):
    """Deliberately ungated: the manifest is app-wide PWA chrome (name, colours,
    icon paths) with nothing token-specific in it, and the PIN screen needs it to
    be installable. It never validated the token to begin with.
    """
    resp = await client.get("/g/pin-token/manifest.json")
    assert resp.status_code == 200
    assert PIN not in resp.text
    assert resp.json()["start_url"] == "/g/pin-token"


# ---------------------------------------------------------------------------
# Unlocking
# ---------------------------------------------------------------------------

async def test_correct_pin_unlocks(client, pin_token, mock_ha_client):
    resp = await client.post("/g/pin-token/pin", data={"pin": PIN})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/g/pin-token"

    # The client jar now holds the cookie; the whole surface opens up.
    assert (await client.get("/g/pin-token/state")).status_code == 200
    assert (await client.get("/g/pin-token/camera/camera.hall")).status_code == 200
    page = await client.get("/g/pin-token")
    assert page.status_code == 200
    assert "Enter PIN" not in page.text

    cmd = await client.post(
        "/g/pin-token/command",
        json={"entity_id": "light.living_room", "service": "turn_on"},
    )
    assert cmd.status_code == 200


async def test_wrong_pin_is_rejected(client, pin_token, mock_ha_client):
    resp = await client.post("/g/pin-token/pin", data={"pin": WRONG_PIN})
    assert resp.status_code == 401
    assert "Incorrect PIN" in resp.text
    assert "set-cookie" not in resp.headers
    assert (await client.get("/g/pin-token/state")).status_code == 401


@pytest.mark.parametrize("bad", ["", "123", "123456789", "abcd", "12 34", "48 21"])
async def test_malformed_pin_is_rejected(client, pin_token, mock_ha_client, bad):
    resp = await client.post("/g/pin-token/pin", data={"pin": bad})
    assert resp.status_code == 401
    assert "set-cookie" not in resp.headers


async def test_pin_post_on_token_without_pin_redirects(client, open_token, mock_ha_client):
    """A bookmarked PIN page still lands on the app after the admin clears it."""
    resp = await client.post("/g/open-token/pin", data={"pin": PIN})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/g/open-token"
    assert "set-cookie" not in resp.headers


async def test_pin_post_on_unknown_slug_is_indistinguishable(client, test_db, mock_ha_client):
    """Same 410 the GET already gives — the POST adds no new signal."""
    resp = await client.post("/g/no-such-slug/pin", data={"pin": PIN})
    assert resp.status_code == 410
    get_resp = await client.get("/g/no-such-slug")
    assert get_resp.status_code == 410


# ---------------------------------------------------------------------------
# Session scoping and invalidation
# ---------------------------------------------------------------------------

async def test_session_for_one_token_does_not_unlock_another(client, test_db, mock_ha_client):
    await _make_token("token-a")
    await _make_token("token-b")

    session = await _unlock(client, "token-a")

    resp = await client.get("/g/token-b/state", headers=_cookie_header(session))
    assert resp.status_code == 401
    assert resp.json() == {"detail": "PIN required"}


async def test_session_cookie_is_scoped_to_its_own_token_path(client, pin_token, mock_ha_client):
    resp = await client.post("/g/pin-token/pin", data={"pin": PIN})
    raw = resp.headers["set-cookie"].lower()
    assert "httponly" in raw
    assert "samesite=lax" in raw
    assert "path=/g/pin-token" in raw
    # Plain HTTP in tests, so no Secure — see the HTTPS case below.
    assert "secure" not in raw


async def test_session_cookie_is_secure_over_https(client, pin_token, mock_ha_client):
    resp = await client.post(
        "/g/pin-token/pin",
        data={"pin": PIN},
        headers={"x-forwarded-proto": "https"},
    )
    assert "secure" in resp.headers["set-cookie"].lower()


async def test_tampered_session_is_rejected(client, pin_token, mock_ha_client):
    session = await _unlock(client, "pin-token")
    version, expires_at, sig = session.split(".")

    # Signature flipped
    forged_sig = f"{version}.{expires_at}.{'A' if sig[0] != 'A' else 'B'}{sig[1:]}"
    resp = await client.get("/g/pin-token/state", headers=_cookie_header(forged_sig))
    assert resp.status_code == 401

    # Expiry pushed out, signature left alone
    forged_exp = f"{version}.{int(expires_at) + 86400}.{sig}"
    resp = await client.get("/g/pin-token/state", headers=_cookie_header(forged_exp))
    assert resp.status_code == 401


async def test_session_does_not_survive_revocation(client, pin_token, mock_ha_client):
    session = await _unlock(client, "pin-token")
    assert (await client.get("/g/pin-token/state")).status_code == 200

    await db.revoke_token(pin_token["id"])

    resp = await client.get("/g/pin-token/state", headers=_cookie_header(session))
    assert resp.status_code == 410
    cam = await client.get("/g/pin-token/camera/camera.hall", headers=_cookie_header(session))
    assert cam.status_code == 410
    mock_ha_client["camera_snapshot"].assert_not_called()


async def test_session_does_not_survive_token_expiry(client, pin_token, mock_ha_client):
    session = await _unlock(client, "pin-token")
    await db.update_token_expiry(pin_token["id"], int(time.time()) - 1)

    resp = await client.get("/g/pin-token/state", headers=_cookie_header(session))
    assert resp.status_code == 410


async def test_session_expiry_is_clamped_to_the_token(client, test_db, mock_ha_client):
    """A short-lived token cannot mint a session that outlives it."""
    token = await _make_token("short-token", expires_in=120)
    session = await _unlock(client, "short-token")
    claimed_expiry = int(session.split(".")[1])
    assert claimed_expiry <= token["expires_at"]


async def test_session_does_not_survive_a_pin_change(client, pin_token, mock_ha_client):
    session = await _unlock(client, "pin-token")
    assert (await client.get("/g/pin-token/state")).status_code == 200

    await db.set_token_pin(pin_token["id"], await guest_pin.hash_pin("9999"))

    resp = await client.get("/g/pin-token/state", headers=_cookie_header(session))
    assert resp.status_code == 401


async def test_session_does_not_survive_clearing_the_pin(client, pin_token, mock_ha_client):
    """Clearing the PIN opens the token, so the old cookie is simply moot —
    what must not happen is it continuing to satisfy a PIN set later."""
    session = await _unlock(client, "pin-token")
    await db.set_token_pin(pin_token["id"], None)
    assert (await client.get("/g/pin-token/state")).status_code == 200

    await db.set_token_pin(pin_token["id"], await guest_pin.hash_pin("9999"))
    resp = await client.get("/g/pin-token/state", headers=_cookie_header(session))
    assert resp.status_code == 401


async def test_reissued_identical_pin_does_not_revive_old_sessions(
    client, pin_token, mock_ha_client
):
    """bcrypt salts per hash, so re-setting the same PIN still rotates the key."""
    session = await _unlock(client, "pin-token")
    await db.set_token_pin(pin_token["id"], await guest_pin.hash_pin(PIN))

    resp = await client.get("/g/pin-token/state", headers=_cookie_header(session))
    assert resp.status_code == 401


async def test_guest_pin_cookie_is_not_admin_auth(client, pin_token, mock_ha_client):
    """A guest PIN session must grant nothing on the admin side."""
    session = await _unlock(client, "pin-token")
    resp = await client.get("/admin/tokens", headers=_cookie_header(session))
    assert resp.status_code == 401

    from app.auth import SESSION_COOKIE as ADMIN_COOKIE

    resp = await client.get(
        "/g/pin-token/state", headers={"Cookie": f"{ADMIN_COOKIE}={session}"}
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Brute-force resistance
# ---------------------------------------------------------------------------

async def test_pin_attempts_are_rate_limited_per_ip(client, pin_token, mock_ha_client):
    burst = PIN_ATTEMPT_LIMITS_PER_IP[0][1]
    for _ in range(burst):
        resp = await client.post("/g/pin-token/pin", data={"pin": WRONG_PIN})
        assert resp.status_code == 401

    resp = await client.post("/g/pin-token/pin", data={"pin": WRONG_PIN})
    assert resp.status_code == 429
    assert "Too many attempts" in resp.text

    # The correct PIN is refused too while the budget is spent — the limiter
    # gates the attempt, not just the failures.
    resp = await client.post("/g/pin-token/pin", data={"pin": PIN})
    assert resp.status_code == 429


async def test_rotating_ips_still_hits_the_per_token_ceiling(client, pin_token, mock_ha_client):
    """Malformed attempts skip bcrypt but must still spend the budget, or the
    per-token ceiling would be free to evade with unparseable input."""
    per_ip = PIN_ATTEMPT_LIMITS_PER_IP[0][1]
    per_token = PIN_ATTEMPT_LIMITS_PER_TOKEN[0][1]

    attempts = 0
    octet = 0
    while attempts < per_token:
        octet += 1
        for _ in range(per_ip):
            if attempts >= per_token:
                break
            resp = await client.post(
                "/g/pin-token/pin",
                data={"pin": "99"},
                headers={"X-Forwarded-For": f"10.0.0.{octet}"},
            )
            assert resp.status_code == 401
            attempts += 1

    # A brand-new address, well inside its own budget, is still refused.
    resp = await client.post(
        "/g/pin-token/pin",
        data={"pin": WRONG_PIN},
        headers={"X-Forwarded-For": "10.0.0.250"},
    )
    assert resp.status_code == 429


async def test_rate_limit_does_not_leak_across_tokens(client, test_db, mock_ha_client):
    """One guest fumbling their PIN must not lock out an unrelated link."""
    await _make_token("noisy-token")
    await _make_token("quiet-token")

    for _ in range(PIN_ATTEMPT_LIMITS_PER_IP[0][1] + 1):
        await client.post("/g/noisy-token/pin", data={"pin": "99"})

    resp = await client.post("/g/quiet-token/pin", data={"pin": PIN})
    assert resp.status_code == 303


# ---------------------------------------------------------------------------
# The PIN never escapes
# ---------------------------------------------------------------------------

async def test_pin_never_reaches_the_access_log_or_activity(
    client, pin_token, mock_ha_client
):
    await _unlock(client, "pin-token")
    await client.get("/g/pin-token")
    await client.post(
        "/g/pin-token/command",
        json={"entity_id": "light.living_room", "service": "turn_on"},
    )

    conn = await db.get_db()
    async with conn.execute("SELECT * FROM access_log") as cur:
        rows = await cur.fetchall()
    assert rows
    for row in rows:
        assert PIN not in " ".join(str(v) for v in tuple(row) if v is not None)

    for call in mock_ha_client["fire_event"].call_args_list:
        assert PIN not in str(call)
    for call in mock_ha_client["logbook_log"].call_args_list:
        assert PIN not in str(call)


async def test_pin_is_not_echoed_by_any_response(client, pin_token, mock_ha_client):
    for resp in (
        await client.post("/g/pin-token/pin", data={"pin": WRONG_PIN}),
        await client.post("/g/pin-token/pin", data={"pin": PIN}),
        await client.get("/g/pin-token"),
    ):
        assert PIN not in resp.text
        assert WRONG_PIN not in resp.text


async def test_pin_is_stored_hashed_not_in_the_clear(test_db, pin_token):
    row = await db.get_token_by_id(pin_token["id"])
    assert row["pin_hash"].startswith("$2")
    assert PIN not in row["pin_hash"]


# ---------------------------------------------------------------------------
# Admin side
# ---------------------------------------------------------------------------

async def test_create_token_with_pin_sets_has_pin(client, admin_session, mock_ha_client):
    resp = await client.post(
        "/admin/tokens",
        json={
            "label": "With PIN",
            "entity_ids": ["light.a"],
            "expires_in_seconds": 3600,
            "pin": PIN,
        },
        cookies=admin_session,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["has_pin"] is True
    assert "pin" not in body
    assert "pin_hash" not in body
    assert PIN not in resp.text

    row = await db.get_token_by_id(body["id"])
    assert row["pin_hash"]


async def test_create_token_without_pin_has_no_pin(client, admin_session, mock_ha_client):
    resp = await client.post(
        "/admin/tokens",
        json={"label": "No PIN", "entity_ids": ["light.a"], "expires_in_seconds": 3600},
        cookies=admin_session,
    )
    assert resp.status_code == 201
    assert resp.json()["has_pin"] is False


@pytest.mark.parametrize("bad", ["123", "123456789", "abcd", "12a4", " 4821 x"])
async def test_create_token_rejects_a_bad_pin(client, admin_session, mock_ha_client, bad):
    resp = await client.post(
        "/admin/tokens",
        json={
            "label": "Bad PIN",
            "entity_ids": ["light.a"],
            "expires_in_seconds": 3600,
            "pin": bad,
        },
        cookies=admin_session,
    )
    assert resp.status_code == 422
    assert "4-8 digits" in resp.json()["detail"]
    # The rejected value must not come back in the error.
    assert bad.strip() not in resp.json()["detail"]


async def test_patch_pin_sets_and_clears(client, admin_session, open_token, mock_ha_client):
    token_id = open_token["id"]

    resp = await client.patch(
        f"/admin/tokens/{token_id}/pin", json={"pin": PIN}, cookies=admin_session
    )
    assert resp.status_code == 200
    assert resp.json() == {"has_pin": True}
    assert (await client.get("/g/open-token/state")).status_code == 401

    resp = await client.patch(
        f"/admin/tokens/{token_id}/pin", json={"pin": None}, cookies=admin_session
    )
    assert resp.status_code == 200
    assert resp.json() == {"has_pin": False}
    assert (await client.get("/g/open-token/state")).status_code == 200


async def test_patch_pin_rejects_a_bad_pin(client, admin_session, pin_token, mock_ha_client):
    resp = await client.patch(
        f"/admin/tokens/{pin_token['id']}/pin",
        json={"pin": "12"},
        cookies=admin_session,
    )
    assert resp.status_code == 422
    row = await db.get_token_by_id(pin_token["id"])
    assert row["pin_hash"]  # unchanged


async def test_patch_pin_requires_admin(client, pin_token, mock_ha_client):
    resp = await client.patch(f"/admin/tokens/{pin_token['id']}/pin", json={"pin": PIN})
    assert resp.status_code == 401


async def test_patch_pin_on_unknown_token_is_404(client, admin_session, test_db, mock_ha_client):
    resp = await client.patch(
        "/admin/tokens/does-not-exist/pin", json={"pin": PIN}, cookies=admin_session
    )
    assert resp.status_code == 404


async def test_token_list_reports_has_pin_but_never_the_value(
    client, admin_session, pin_token, mock_ha_client
):
    resp = await client.get("/admin/tokens", cookies=admin_session)
    assert resp.status_code == 200
    entry = next(t for t in resp.json() if t["id"] == pin_token["id"])
    assert entry["has_pin"] is True
    assert PIN not in resp.text
