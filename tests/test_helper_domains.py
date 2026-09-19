"""Tests for the alarm, button, date/time and HA helper domains.

Same integration shape as test_guest_security.py: a real token in the real DB,
the real router and allowlist, only ha_client mocked. Each new domain is checked
three ways — its own services reach HA, a service it does not own is refused,
and the FORBIDDEN_DATA_KEYS scrub still applies to its payloads.
"""
import time

import pytest

from app import database as db
from app.models import ALLOWED_SERVICES, FORBIDDEN_DATA_KEYS, READ_ONLY_DOMAINS

# (domain, entity_id, allowed service, a service this domain must not have)
DOMAIN_CASES = [
    ("alarm_control_panel", "alarm_control_panel.house", "alarm_arm_home", "alarm_trigger"),
    ("button", "button.doorbell", "press", "turn_on"),
    ("time", "time.wake_up", "set_value", "set_datetime"),
    ("datetime", "datetime.next_visit", "set_value", "set_datetime"),
    ("group", "group.downstairs", "toggle", "set_value"),
    ("input_number", "input_number.volume", "set_value", "increment"),
    ("input_text", "input_text.note", "set_value", "select_option"),
    ("input_select", "input_select.mode", "select_option", "set_value"),
    ("input_datetime", "input_datetime.checkout", "set_datetime", "set_value"),
    ("input_button", "input_button.doorbell", "press", "turn_on"),
    ("counter", "counter.visits", "increment", "configure"),
    ("timer", "timer.laundry", "start", "finish"),
]


async def _token_for(entity_id: str, slug: str) -> None:
    """Create a token whose only entity is entity_id."""
    now = int(time.time())
    await db.create_token(
        label=f"Token for {entity_id}", slug=slug, entity_ids=[entity_id],
        expires_at=now + 3600, ip_allowlist=None,
    )


# ---------------------------------------------------------------------------
# ALLOWED_SERVICES — one allowed and one refused service per new domain
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("domain,entity_id,allowed,_refused", DOMAIN_CASES)
async def test_allowed_service_reaches_ha(
    client, mock_ha_client, test_db, domain, entity_id, allowed, _refused
):
    assert allowed in ALLOWED_SERVICES[domain]
    await _token_for(entity_id, f"ok-{domain}")
    resp = await client.post(
        f"/g/ok-{domain}/command",
        json={"entity_id": entity_id, "service": f"{domain}.{allowed}"},
    )
    assert resp.status_code == 200
    args = mock_ha_client["call_service"].call_args[0]
    assert args[0] == domain
    assert args[1] == allowed
    assert args[2]["entity_id"] == entity_id


@pytest.mark.parametrize("domain,entity_id,_allowed,refused", DOMAIN_CASES)
async def test_service_outside_allowlist_never_reaches_ha(
    client, mock_ha_client, test_db, domain, entity_id, _allowed, refused
):
    assert refused not in ALLOWED_SERVICES[domain]
    await _token_for(entity_id, f"no-{domain}")
    resp = await client.post(
        f"/g/no-{domain}/command",
        json={"entity_id": entity_id, "service": f"{domain}.{refused}"},
    )
    assert resp.status_code == 403
    mock_ha_client["call_service"].assert_not_called()


@pytest.mark.parametrize("domain,entity_id,allowed,_refused", DOMAIN_CASES)
async def test_forbidden_data_keys_scrubbed(
    client, mock_ha_client, test_db, domain, entity_id, allowed, _refused
):
    """The entity-allowlist guard applies to every new domain's payload."""
    data = {key: "injected" for key in FORBIDDEN_DATA_KEYS}
    data["value"] = 1  # legitimate payload for whichever service is under test

    await _token_for(entity_id, f"scrub-{domain}")
    resp = await client.post(
        f"/g/scrub-{domain}/command",
        json={"entity_id": entity_id, "service": f"{domain}.{allowed}", "data": data},
    )
    assert resp.status_code == 200
    service_data = mock_ha_client["call_service"].call_args[0][2]
    assert service_data["entity_id"] == entity_id  # the token's entity, not the injected one
    for key in FORBIDDEN_DATA_KEYS - {"entity_id"}:
        assert key not in service_data
    assert service_data["value"] == 1


# ---------------------------------------------------------------------------
# Alarm panels
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "service", ["alarm_arm_home", "alarm_arm_away", "alarm_arm_night", "alarm_disarm"]
)
async def test_alarm_arm_modes_and_disarm_allowed(client, mock_ha_client, test_db, service):
    await _token_for("alarm_control_panel.house", f"alarm-{service}")
    resp = await client.post(
        f"/g/alarm-{service}/command",
        json={"entity_id": "alarm_control_panel.house", "service": service},
    )
    assert resp.status_code == 200
    args = mock_ha_client["call_service"].call_args[0]
    assert args[0] == "alarm_control_panel"
    assert args[1] == service


async def test_alarm_trigger_never_reaches_ha(client, mock_ha_client, test_db):
    """Setting off the siren is refused for the same reason script/scene are."""
    assert "alarm_trigger" not in ALLOWED_SERVICES["alarm_control_panel"]
    await _token_for("alarm_control_panel.house", "alarm-trigger")
    resp = await client.post(
        "/g/alarm-trigger/command",
        json={"entity_id": "alarm_control_panel.house", "service": "alarm_control_panel.alarm_trigger"},
    )
    assert resp.status_code == 403
    mock_ha_client["call_service"].assert_not_called()


async def test_alarm_code_forwarded_as_service_field(client, mock_ha_client, test_db):
    """A guest-typed code reaches HA as `code` and is not written to the log."""
    await _token_for("alarm_control_panel.house", "alarm-code")
    resp = await client.post(
        "/g/alarm-code/command",
        json={
            "entity_id": "alarm_control_panel.house",
            "service": "alarm_control_panel.alarm_disarm",
            "data": {"code": "1234"},
        },
    )
    assert resp.status_code == 200
    service_data = mock_ha_client["call_service"].call_args[0][2]
    assert service_data["code"] == "1234"

    conn = await db.get_db()
    async with conn.execute("SELECT * FROM access_log WHERE event_type = 'command'") as cur:
        row = await cur.fetchone()
    assert row["entity_id"] == "alarm_control_panel.house"
    assert "1234" not in str(tuple(row))

    _event_type, payload = mock_ha_client["fire_event"].call_args[0]
    assert "1234" not in str(payload)


# ---------------------------------------------------------------------------
# schedule — read-only, like sensor and camera
# ---------------------------------------------------------------------------

async def test_schedule_domain_is_read_only(client, mock_ha_client, test_db):
    assert "schedule" in READ_ONLY_DOMAINS
    assert "schedule" not in ALLOWED_SERVICES
    await _token_for("schedule.cleaning", "schedule-test")
    for service in ("turn_on", "schedule.reload", "get_schedule"):
        resp = await client.post(
            "/g/schedule-test/command",
            json={"entity_id": "schedule.cleaning", "service": service},
        )
        assert resp.status_code == 403
    mock_ha_client["call_service"].assert_not_called()
