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
}

# camera is read-only on purpose: it is deliberately absent from ALLOWED_SERVICES,
# so every camera.* service call is rejected by the domain check in the command
# handler. Guests get pixels, never control.
READ_ONLY_DOMAINS: set[str] = {"sensor", "binary_sensor", "camera"}
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
# show_brightness is opt-IN: with no option set a light gets on/off only, and a
# token has to enable the slider per entity.
ENTITY_OPTION_KEYS: set[str] = {"show_brightness"}


class TokenCreateRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=200)
    slug: str | None = Field(default=None, pattern=r"^[a-z0-9_-]{1,64}$")
    entity_ids: list[str] = Field(..., min_length=1)
    expires_in_seconds: int = Field(..., gt=0)
    ip_allowlist: list[str] | None = None
    entity_meta: dict[str, dict[str, Any]] | None = None


class TokenUpdateEntitiesRequest(BaseModel):
    entity_ids: list[str] = Field(..., min_length=1)
    entity_meta: dict[str, dict[str, Any]] | None = None


class EntityMetaRequest(BaseModel):
    """Set one entity's presentation overrides.

    A blank display_name clears the override and falls back to the HA
    friendly_name. Unknown option keys are dropped, not rejected.
    """
    entity_id: str = Field(..., min_length=1, max_length=255)
    display_name: str | None = Field(default=None, max_length=DISPLAY_NAME_MAX)
    options: dict[str, Any] | None = None


class TokenUpdateExpiryRequest(BaseModel):
    expires_in_seconds: int = Field(..., gt=0)


class CommandRequest(BaseModel):
    entity_id: str
    service: str  # e.g. "light.turn_on"
    data: dict[str, Any] = Field(default_factory=dict)


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
