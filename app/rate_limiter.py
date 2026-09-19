"""Per-token sliding-window rate limiter (in-memory)."""
import asyncio
import time
from collections import deque
from collections.abc import Sequence

# Each window is approximated by a ring of fixed-width buckets instead of raw
# per-request timestamps. An hour-long window at a few requests per second would
# otherwise retain thousands of floats per token; buckets bound that at
# BUCKETS_PER_WINDOW entries no matter how fast the guest clicks. The cost is
# resolution at the trailing edge: a bucket is dropped only once it lies wholly
# outside the window, so the limiter may count up to one bucket of extra history
# — it errs strict, never loose, by at most 1/BUCKETS_PER_WINDOW of the window.
BUCKETS_PER_WINDOW = 60


class _WindowState:
    """Bucketed request count for one (token, window) pair.

    `count` is the running total of the buckets, maintained on append/evict so
    the hot path never rescans the deque.
    """

    __slots__ = ("width", "window", "buckets", "count")

    def __init__(self, window: float) -> None:
        self.window = window
        self.width = window / BUCKETS_PER_WINDOW
        # (bucket_start, count) pairs, oldest first
        self.buckets: deque[list[float]] = deque()
        self.count = 0

    def expire(self, now: float) -> None:
        """Drop buckets that ended before the window opened."""
        cutoff = now - self.window
        while self.buckets and self.buckets[0][0] + self.width <= cutoff:
            self.count -= self.buckets.popleft()[1]

    def record(self, now: float) -> None:
        start = now - (now % self.width)
        if not self.buckets or self.buckets[-1][0] != start:
            self.buckets.append([start, 0])
        self.buckets[-1][1] += 1
        self.count += 1


class RateLimiter:
    WINDOW_SECONDS = 60.0

    def __init__(self) -> None:
        self._windows: dict[str, dict[float, _WindowState]] = {}
        self._lock = asyncio.Lock()

    async def check(self, token_id: str, limit_rpm: int) -> bool:
        """Return True if the request is allowed, False if rate-limited."""
        return await self.check_multi(token_id, ((self.WINDOW_SECONDS, limit_rpm),))

    async def check_multi(self, token_id: str, limits: Sequence[tuple[float, int]]) -> bool:
        """Return True only if the request is allowed under EVERY given
        (window_seconds, limit) constraint — e.g. a short burst allowance plus a
        lower sustained-rate cap over a longer window.

        Nothing is recorded unless every constraint passes, so a rejected request
        does not eat into the wider windows.
        """
        now = time.monotonic()

        async with self._lock:
            states = self._windows.setdefault(token_id, {})
            passed = []
            for window, limit in limits:
                state = states.get(window)
                if state is None:
                    state = states[window] = _WindowState(window)
                state.expire(now)
                if state.count >= limit:
                    return False
                passed.append(state)

            for state in passed:
                state.record(now)
            return True

    async def cleanup(self) -> None:
        """Remove entries for tokens with no recent requests (call periodically).

        Retention is the widest window the token actually uses, not a fixed
        horizon: dropping a token while a request of its is still inside, say, an
        hour-long sustained window would silently reset that cap on any idle gap
        longer than the cleanup interval.
        """
        now = time.monotonic()
        async with self._lock:
            stale = []
            for tid, states in self._windows.items():
                for state in states.values():
                    state.expire(now)
                if all(state.count == 0 for state in states.values()):
                    stale.append(tid)
            for tid in stale:
                del self._windows[tid]


rate_limiter = RateLimiter()
