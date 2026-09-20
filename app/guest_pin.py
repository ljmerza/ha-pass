"""Guest PIN hashing and the signed session a correct entry hands back.

Deliberately separate from app/auth.py: that module owns the admin password and
the admin session cookie, and nothing a guest does may touch either. The two
share only bcrypt, used the same way — hash once, verify off the event loop.

Storage is write-only. A PIN is bcrypt-hashed on the way in and never decrypted,
so the admin UI can report whether a PIN is set but can never show it; a
forgotten PIN is replaced, not recovered.
"""
import asyncio
import base64
import hashlib
import hmac
import re
import time

import bcrypt

# Numeric, 4-8 digits. The guest enters this on a phone, so a digits-only policy
# buys a keypad (inputmode="numeric") instead of a full keyboard — and the PIN is
# a second factor behind a 128-bit random slug the attacker must already hold,
# not a standalone credential. 4 digits is the floor most people will actually
# use; 8 is there for an admin who wants a link that never expires to cost more
# than 10^4 guesses. Everything outside the range is refused server-side so the
# guest keypad can never produce a PIN the admin's keyboard accepted.
PIN_MIN_LENGTH = 4
PIN_MAX_LENGTH = 8
_PIN_RE = re.compile(rf"^\d{{{PIN_MIN_LENGTH},{PIN_MAX_LENGTH}}}$")

SESSION_COOKIE = "ha_guest_pin_session"

# How long one correct entry lasts before the guest is asked again. Matches the
# admin session lifetime; always clamped down to the token's own expiry.
SESSION_TTL_SECONDS = 86400

_SESSION_VERSION = "v1"
_SESSION_KEY_INFO = b"hapass-guest-pin-session-v1"


def is_valid_pin(pin: str) -> bool:
    return bool(_PIN_RE.match(pin))


async def hash_pin(pin: str) -> str:
    """bcrypt hash of a PIN. CPU-bound, so it runs off the event loop."""
    loop = asyncio.get_running_loop()
    hashed = await loop.run_in_executor(None, bcrypt.hashpw, pin.encode(), bcrypt.gensalt())
    return hashed.decode()


async def verify_pin(pin: str, pin_hash: str) -> bool:
    """Check a submitted PIN. bcrypt.checkpw does the comparison in constant
    time — there is deliberately no `==` anywhere in this module's hot path.
    """
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            None, bcrypt.checkpw, pin.encode(), pin_hash.encode()
        )
    except ValueError:
        # Hash unreadable (hand-edited row, truncated column). Refuse rather
        # than 500 — a broken hash must fail closed, not open.
        return False


def _session_key(pin_hash: str) -> bytes:
    """Per-token HMAC key derived from that token's own bcrypt hash.

    Three of the session requirements fall out of this one choice and need no
    extra bookkeeping: the key is distinct per token (a cookie minted for token A
    cannot verify against token B), and it changes the instant the PIN is changed
    or cleared (outstanding sessions stop verifying). The hash carries 128 bits
    of bcrypt salt, never leaves the server, and is never returned by the admin
    API, so it is suitable key material; the info string keeps this use
    domain-separated from the hash's own purpose.
    """
    return hmac.new(pin_hash.encode(), _SESSION_KEY_INFO, hashlib.sha256).digest()


def _signature(token_id: str, expires_at: int, pin_hash: str) -> str:
    msg = f"{_SESSION_VERSION}|{token_id}|{expires_at}".encode()
    digest = hmac.new(_session_key(pin_hash), msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def issue_session(token_id: str, pin_hash: str, token_expires_at: int) -> tuple[str, int]:
    """Mint a session cookie value for a token. Returns (value, max_age).

    The expiry claim is clamped to the token's own expiry, so the cookie cannot
    outlive the link even on a token that never expires. The guest endpoints
    re-read the token on every request anyway, so a link revoked or shortened
    after the fact still fails immediately — this clamp is the cheap half.
    """
    now = int(time.time())
    expires_at = min(now + SESSION_TTL_SECONDS, token_expires_at)
    value = f"{_SESSION_VERSION}.{expires_at}.{_signature(token_id, expires_at, pin_hash)}"
    return value, max(expires_at - now, 0)


def verify_session(cookie: str | None, token_id: str, pin_hash: str) -> bool:
    """True only if `cookie` is a live session this token's current PIN signed."""
    if not cookie:
        return False
    parts = cookie.split(".")
    if len(parts) != 3 or parts[0] != _SESSION_VERSION:
        return False
    try:
        expires_at = int(parts[1])
    except ValueError:
        return False
    if expires_at <= int(time.time()):
        return False
    return hmac.compare_digest(parts[2], _signature(token_id, expires_at, pin_hash))
