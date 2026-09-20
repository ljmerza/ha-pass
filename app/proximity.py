"""Arithmetic behind the per-entity proximity gate.

What this is: the guest's browser reports where it thinks it is, and the server
compares that against HA's zone.home itself. What it is not: proof that anyone
is at the door. The coordinates are self-reported, so a guest willing to edit
the request can claim any position — the same caveat the IP allowlist carries.
It raises the bar for a casual guest using the link from elsewhere, nothing
more, and nothing in the UI or the API should promise otherwise.

Deliberately free of I/O: the router fetches zone.home and supplies the clock,
this holds only the distance and freshness rules so both are testable on their
own.
"""
import math

# Mean Earth radius. The gate compares against a zone radius measured in tens or
# hundreds of metres, so a spherical model is well inside the tolerance that
# matters here.
EARTH_RADIUS_METERS = 6371000

# How old a fix may be, in seconds, measured against the server's clock.
#
# Generous enough for a cold high-accuracy fix (10-20s is ordinary), a tab the
# phone backgrounded mid-request, and a slow mobile network. Short enough that a
# request body captured once does not keep working: replaying yesterday's
# coordinates fails on the timestamp, so an attacker has to forge that too.
# Forging it is easy — this is a speed bump on replay, not a defence against a
# guest who edits requests.
#
# It does mean a device whose clock is badly wrong can never pass. Phone and
# desktop clocks are network-synced in practice, and refusing a fix we cannot
# place in time is the right side to fail on for an access control.
MAX_FIX_AGE_SECONDS = 120

# Fixes stamped a little in the future are tolerated rather than rejected — a
# device a few seconds ahead of the server is common and means nothing.
MAX_FIX_SKEW_SECONDS = 30

# Slack on the radius comparison, in metres, so a point computed to lie exactly
# on the boundary lands inside it rather than on whichever side the last bit of
# the floating-point result happens to fall. A micrometre against a radius
# measured in metres and a fix accurate to several of them.
BOUNDARY_TOLERANCE_METERS = 1e-6


def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/long points, in metres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_METERS * math.asin(math.sqrt(a))


def fix_is_fresh(timestamp_ms: int, now: float) -> bool:
    """True if a fix stamped `timestamp_ms` is recent enough to act on.

    `timestamp_ms` is GeolocationPosition.timestamp — milliseconds since the
    epoch on the guest's device — and `now` is the server's wall clock in
    seconds.
    """
    age = now - (timestamp_ms / 1000.0)
    return -MAX_FIX_SKEW_SECONDS <= age <= MAX_FIX_AGE_SECONDS


def is_within_zone(latitude: float, longitude: float, zone: dict[str, float]) -> bool:
    """True if a point lies inside zone.home.

    The radius is inclusive: a fix landing exactly on the boundary counts as
    inside. GPS error dwarfs the difference either way, and an inclusive edge
    means an admin who sets the radius to the distance they measured gets the
    behaviour they expect.
    """
    distance = haversine_meters(
        latitude, longitude, zone["latitude"], zone["longitude"]
    )
    return distance <= zone["radius"] + BOUNDARY_TOLERANCE_METERS
