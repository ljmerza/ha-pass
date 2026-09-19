"""Tests for the in-memory sliding-window rate limiter."""
import time
from unittest.mock import patch

import pytest

from app.rate_limiter import BUCKETS_PER_WINDOW, RateLimiter


@pytest.fixture
def limiter():
    return RateLimiter()


async def test_allows_within_limit(limiter):
    """N requests under limit all return True."""
    for _ in range(5):
        assert await limiter.check("token-a", 5) is True


async def test_blocks_over_limit(limiter):
    """Request N+1 returns False."""
    for _ in range(5):
        await limiter.check("token-a", 5)
    assert await limiter.check("token-a", 5) is False


async def test_window_slides(limiter):
    """After 60s, old requests expire and new ones are allowed."""
    base = time.monotonic()
    with patch("time.monotonic", return_value=base):
        for _ in range(5):
            await limiter.check("token-a", 5)

    # Advance past the 60-second window
    with patch("time.monotonic", return_value=base + 61):
        assert await limiter.check("token-a", 5) is True


async def test_window_does_not_slide_prematurely(limiter):
    """At exactly 59s, old requests should still count."""
    base = time.monotonic()
    with patch("time.monotonic", return_value=base):
        for _ in range(5):
            await limiter.check("token-a", 5)

    # 59 seconds later — still within the 60-second window
    with patch("time.monotonic", return_value=base + 59):
        assert await limiter.check("token-a", 5) is False


async def test_different_tokens_independent(limiter):
    """Token A's limit doesn't affect token B."""
    for _ in range(5):
        await limiter.check("token-a", 5)
    assert await limiter.check("token-a", 5) is False
    assert await limiter.check("token-b", 5) is True


async def test_cleanup_removes_stale(limiter):
    """cleanup() removes tokens with no recent activity."""
    base = time.monotonic()
    with patch("time.monotonic", return_value=base):
        await limiter.check("token-a", 10)

    with patch("time.monotonic", return_value=base + 61):
        await limiter.cleanup()
        assert "token-a" not in limiter._windows


async def test_cleanup_keeps_active(limiter):
    """Active tokens survive cleanup."""
    await limiter.check("token-a", 10)
    await limiter.cleanup()
    assert "token-a" in limiter._windows


async def test_limit_of_one(limiter):
    """RPM=1 allows exactly one request."""
    assert await limiter.check("token-a", 1) is True
    assert await limiter.check("token-a", 1) is False


async def test_partial_window_expiry(limiter):
    """Only old entries expire; recent ones remain and count."""
    base = time.monotonic()

    # 3 requests at t=0
    with patch("time.monotonic", return_value=base):
        for _ in range(3):
            await limiter.check("token-a", 5)

    # 2 more requests at t=30 (within window)
    with patch("time.monotonic", return_value=base + 30):
        for _ in range(2):
            await limiter.check("token-a", 5)

    # At t=61, the first 3 expired but the 2 from t=30 are still in window
    with patch("time.monotonic", return_value=base + 61):
        assert await limiter.check("token-a", 5) is True  # 2 in window, under 5
        assert await limiter.check("token-a", 5) is True  # 3 in window
        assert await limiter.check("token-a", 5) is True  # 4 in window
        assert await limiter.check("token-a", 5) is False  # 5 = limit, next blocked


# ---------------------------------------------------------------------------
# Multi-window limiting
# ---------------------------------------------------------------------------

# A burst allowance plus a lower sustained cap, as the guest command path uses.
BURST_AND_SUSTAINED = ((60.0, 10), (3600.0, 25))


async def test_check_multi_enforces_burst_window(limiter):
    """The short window blocks once its own limit is hit, sustained untouched."""
    base = time.monotonic()
    with patch("time.monotonic", return_value=base):
        for _ in range(10):
            assert await limiter.check_multi("token-a", BURST_AND_SUSTAINED) is True
        assert await limiter.check_multi("token-a", BURST_AND_SUSTAINED) is False


async def test_check_multi_enforces_sustained_window(limiter):
    """The long window blocks even though the short window was never exceeded."""
    limits = ((60.0, 100), (3600.0, 10))
    base = time.monotonic()
    with patch("time.monotonic", return_value=base):
        for _ in range(10):
            assert await limiter.check_multi("token-a", limits) is True
        # 10 requests in one second is nowhere near the 100/min burst cap
        assert await limiter.check_multi("token-a", limits) is False


async def test_check_multi_passes_burst_but_fails_sustained(limiter):
    """A request inside a fresh burst window is still rejected by the hour cap."""
    limits = ((60.0, 10), (3600.0, 20))
    base = time.monotonic()

    with patch("time.monotonic", return_value=base):
        for _ in range(10):
            await limiter.check_multi("token-a", limits)

    with patch("time.monotonic", return_value=base + 61):
        for _ in range(10):
            assert await limiter.check_multi("token-a", limits) is True

    # t=122: the burst window is empty again, but 20 requests are still inside
    # the hour, so the sustained cap is what rejects this one.
    with patch("time.monotonic", return_value=base + 122):
        assert await limiter.check_multi("token-a", limits) is False


async def test_check_multi_windows_expire(limiter):
    """Each window frees up independently as its own horizon passes."""
    limits = ((60.0, 10), (3600.0, 10))
    base = time.monotonic()

    with patch("time.monotonic", return_value=base):
        for _ in range(10):
            await limiter.check_multi("token-a", limits)

    # Burst window clear, hour window still full
    with patch("time.monotonic", return_value=base + 61):
        assert await limiter.check_multi("token-a", limits) is False

    with patch("time.monotonic", return_value=base + 3661):
        assert await limiter.check_multi("token-a", limits) is True


async def test_rejected_request_is_not_recorded(limiter):
    """A request blocked by one window must not count against the others."""
    limits = ((60.0, 2), (3600.0, 10))
    base = time.monotonic()

    with patch("time.monotonic", return_value=base):
        assert await limiter.check_multi("token-a", limits) is True
        assert await limiter.check_multi("token-a", limits) is True
        assert await limiter.check_multi("token-a", limits) is False

    # The hour window saw 2 requests, not 3 — so a cap of 3 allows exactly one more.
    with patch("time.monotonic", return_value=base + 61):
        assert await limiter.check_multi("token-a", ((60.0, 10), (3600.0, 3))) is True
        assert await limiter.check_multi("token-a", ((60.0, 10), (3600.0, 3))) is False


async def test_check_delegates_to_single_60s_window(limiter):
    """check() is the one-minute case of check_multi() — same underlying state."""
    base = time.monotonic()
    with patch("time.monotonic", return_value=base):
        for _ in range(5):
            assert await limiter.check("token-a", 5) is True
        assert await limiter.check_multi("token-a", ((60.0, 5),)) is False


# ---------------------------------------------------------------------------
# Cleanup retention
# ---------------------------------------------------------------------------

async def test_cleanup_keeps_token_inside_widest_window(limiter):
    """Retention must cover the widest window, not just 60s.

    Regression: cleanup() used to drop any token idle for 60s. With an hour-long
    sustained cap in play that silently reset it on every short idle gap.
    """
    base = time.monotonic()
    with patch("time.monotonic", return_value=base):
        for _ in range(10):
            await limiter.check_multi("token-a", BURST_AND_SUSTAINED)

    # 5 minutes idle: well past the burst window, well inside the hour
    with patch("time.monotonic", return_value=base + 300):
        await limiter.cleanup()
        assert "token-a" in limiter._windows
        # ...and the sustained count survived, so a cap of 10 is already spent
        assert await limiter.check_multi("token-a", ((60.0, 10), (3600.0, 10))) is False


async def test_cleanup_removes_token_past_widest_window(limiter):
    """Once the hour window empties, the token is dropped."""
    base = time.monotonic()
    with patch("time.monotonic", return_value=base):
        await limiter.check_multi("token-a", BURST_AND_SUSTAINED)

    # 3600 + one bucket width: a bucket is only dropped once it lies wholly
    # outside the window, so the effective horizon is a bucket longer.
    with patch("time.monotonic", return_value=base + 3661):
        await limiter.cleanup()
        assert "token-a" not in limiter._windows


# ---------------------------------------------------------------------------
# Memory shape
# ---------------------------------------------------------------------------

async def test_bucket_count_bounded_by_resolution(limiter):
    """Retained buckets stay bounded no matter how many requests land."""
    base = time.monotonic()
    with patch("time.monotonic") as mono:
        for i in range(600):
            mono.return_value = base + i * 3.0
            await limiter.check_multi("token-a", ((3600.0, 100_000),))

    state = limiter._windows["token-a"][3600.0]
    assert state.count == 600
    assert len(state.buckets) <= BUCKETS_PER_WINDOW + 1
