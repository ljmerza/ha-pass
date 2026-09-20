"""Pydantic request/response models."""
from typing import Any
from pydantic import BaseModel, Field

NEVER_EXPIRES_SECONDS = 4102444800  # 2099-12-31T00:00:00Z

# Services guests are permitted to call, keyed by entity domain.
# Script/scene/automation domains are intentionally excluded —
# they execute arbitrary automations and bypass entity scoping.
ALLOWED_SERVICES: dict[str, set[str]] = {
    "light":         {"turn_on", "turn_off", "toggle"},
    "switch":        {"turn_on", "turn_off", "toggle"},
    "input_boolean": {"turn_on", "turn_off", "toggle"},
    "climate":       {"set_temperature", "set_hvac_mode", "turn_on", "turn_off"},
    "lock":          {"lock", "unlock", "open"},
    "media_player":  {"media_play", "media_pause", "media_stop", "volume_set",
                      "media_play_pause", "turn_on", "turn_off"},
    "cover":         {"open_cover", "close_cover", "stop_cover"},
    "fan":           {"turn_on", "turn_off", "toggle", "set_percentage"},
    "group":         {"turn_on", "turn_off", "toggle"},
    "button":        {"press"},
    "time":          {"set_value"},
    "datetime":      {"set_value"},
    # alarm_trigger is excluded for the same reason as script/scene: it lets a
    # guest link set off the siren remotely, which no arm/disarm widget needs.
    "alarm_control_panel": {"alarm_arm_home", "alarm_arm_away",
                            "alarm_arm_night", "alarm_disarm"},
    # Helper domains (Settings -> Devices & Services -> Helpers). Each lists
    # exactly the services its guest widget calls, nothing more.
    "input_number":   {"set_value"},
    "input_text":     {"set_value"},
    "input_select":   {"select_option"},
    "input_datetime": {"set_datetime"},
    "input_button":   {"press"},
    "counter":        {"increment", "decrement", "reset"},
    "timer":          {"start", "pause", "cancel"},
}

# camera is read-only on purpose: it is deliberately absent from ALLOWED_SERVICES,
# so every camera.* service call is rejected by the domain check in the command
# handler. Guests get pixels, never control.
# schedule is read-only for a different reason: its only services rewrite the
# weekly schedule wholesale, which is not a safe one-tap guest action.
READ_ONLY_DOMAINS: set[str] = {"sensor", "binary_sensor", "camera", "schedule"}
SUPPORTED_DOMAINS: set[str] = set(ALLOWED_SERVICES) | READ_ONLY_DOMAINS

# Keys that could bypass the entity allowlist if forwarded to HA
FORBIDDEN_DATA_KEYS = {"entity_id", "device_id", "area_id", "floor_id", "label_id"}


class AdminLoginRequest(BaseModel):
    username: str
    password: str


# Per-token presentation overrides. A display name is free text shown to the
# guest, so it is length-capped here and escaped at render time.
DISPLAY_NAME_MAX = 64

# Per-entity display toggles. Allow-listed so an arbitrary JSON blob from the
# admin API can't accumulate keys nothing renders. Presentation only — nothing in
# the command path reads these.
#
# show_brightness and show_color are opt-IN: with no option set a light gets
# on/off only, and a token has to enable the slider or the colour wheel per
# entity. Neither gates the command path: putting a light on a token is what
# grants light.turn_on, so these only decide what the guest UI draws.
ENTITY_OPTION_KEYS: set[str] = {"show_brightness", "show_color"}

# Colour keys HA's light.turn_on understands. Only rgb_color is accepted from a
# guest: it is the one the colour wheel sends, and every other format would need
# its own range check before it could be forwarded safely.
LIGHT_COLOR_KEYS: set[str] = {
    "rgb_color", "rgbw_color", "rgbww_color", "hs_color", "xy_color",
    "color_temp", "color_temp_kelvin", "color_name", "profile", "white",
}
GUEST_COLOR_KEY = "rgb_color"


def validate_light_color(data: dict[str, Any]) -> str | None:
    """Check a guest light.turn_on payload's colour. Returns an error, or None.

    The wheel posts whatever the guest's pointer produced, so the value is
    shape- and range-checked here rather than handed to HA as-is.
    """
    for key in data:
        if key in LIGHT_COLOR_KEYS and key != GUEST_COLOR_KEY:
            return f"Colour format '{key}' is not accepted"
    if GUEST_COLOR_KEY not in data:
        return None
    # Membership, not .get() — an explicit null is a malformed colour, not an
    # absent one, and must not be forwarded as a null key.
    rgb = data[GUEST_COLOR_KEY]
    if not isinstance(rgb, (list, tuple)) or len(rgb) != 3:
        return "rgb_color must be three values"
    for channel in rgb:
        # bool is an int subclass, so True would otherwise pass the range check.
        if isinstance(channel, bool) or not isinstance(channel, int) or not 0 <= channel <= 255:
            return "rgb_color values must be integers from 0 to 255"
    return None


class TokenCreateRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=200)
    slug: str | None = Field(default=None, pattern=r"^[a-z0-9_-]{1,64}$")
    entity_ids: list[str] = Field(..., min_length=1)
    expires_in_seconds: int = Field(..., gt=0)
    ip_allowlist: list[str] | None = None
    entity_meta: dict[str, dict[str, Any]] | None = None
    # Deliberately unconstrained here and validated in the router instead: a
    # Field(pattern=...) rejection becomes a 422 whose body echoes the offending
    # `input` back, which for this one field would put the PIN in a response.
    pin: str | None = None


class TokenPinRequest(BaseModel):
    """Set, replace, or clear a token's PIN. Null or blank clears it.

    Unconstrained for the same reason as TokenCreateRequest.pin.
    """
    pin: str | None = None


class TokenUpdateEntitiesRequest(BaseModel):
    entity_ids: list[str] = Field(..., min_length=1)
    entity_meta: dict[str, dict[str, Any]] | None = None


class EntityMetaRequest(BaseModel):
    """Set one entity's presentation overrides and its proximity gate.

    A blank display_name clears the override and falls back to the HA
    friendly_name. Unknown option keys are dropped, not rejected.

    require_proximity is a sibling of `options`, not a member of it: it is an
    access control the command path enforces, and `options` is the blob nothing
    in that path reads.
    """
    entity_id: str = Field(..., min_length=1, max_length=255)
    display_name: str | None = Field(default=None, max_length=DISPLAY_NAME_MAX)
    options: dict[str, Any] | None = None
    require_proximity: bool = False


class TokenUpdateExpiryRequest(BaseModel):
    expires_in_seconds: int = Field(..., gt=0)


class GuestLocation(BaseModel):
    """Where the guest's browser says it is, for a proximity-gated command.

    Coordinates only — there is no "I am at home" flag to send, because the
    server does the comparison against zone.home itself and a client-asserted
    answer would be worth nothing. `timestamp` is GeolocationPosition.timestamp
    (milliseconds since the epoch) and is required: a fix with no time on it
    cannot be checked for staleness, and one that cannot be checked is refused.
    """
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    timestamp: int


class CommandRequest(BaseModel):
    entity_id: str
    service: str  # e.g. "light.turn_on"
    data: dict[str, Any] = Field(default_factory=dict)
    # Only sent for entities the token marks as proximity-gated. Absent on every
    # other command, which is why it defaults to None rather than being required.
    location: GuestLocation | None = None


class TokenResponse(BaseModel):
    id: str
    slug: str
    label: str
    created_at: int
    expires_at: int
    revoked: bool
    last_accessed: int | None
    ip_allowlist: list[str] | None
    entity_count: int
    entity_ids: list[str] | None = None
