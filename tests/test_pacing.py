"""RateLimiter (sliding windows, wait vs raise) and TypingDelay."""

from __future__ import annotations

import random
import pytest

from tests.conftest import FakeClock
from whatsapp_web import (
    InMemorySendHistory,
    RateLimitConfig,
    RateLimitExceededError,
    RateLimiter,
    SafetyViolationError,
    TypingConfig,
    TypingDelay,
)


def make_limiter(clock: FakeClock, **options) -> RateLimiter:
    defaults = {"min_interval_seconds": 0, "max_per_minute": 100, "max_per_hour": 1000, "max_per_day": 10_000}
    return RateLimiter(RateLimitConfig(**{**defaults, **options}), clock.as_clock())


class TestRateLimiter:
    async def test_first_send_is_immediate(self):
        clock = FakeClock()
        await make_limiter(clock, min_interval_seconds=3).acquire()
        assert clock.sleeps == []

    async def test_min_interval_waits_remaining_time(self):
        clock = FakeClock()
        limiter = make_limiter(clock, min_interval_seconds=3)
        await limiter.acquire()
        clock.time += 1
        await limiter.acquire()
        assert clock.sleeps == [pytest.approx(2.0)]

    async def test_min_interval_boundary_needs_no_wait(self):
        clock = FakeClock()
        limiter = make_limiter(clock, min_interval_seconds=3)
        await limiter.acquire()
        clock.time += 3
        await limiter.acquire()
        assert clock.sleeps == []

    async def test_minute_window_blocks_until_oldest_expires(self):
        clock = FakeClock()
        limiter = make_limiter(clock, max_per_minute=2)
        await limiter.acquire()
        clock.time += 10
        await limiter.acquire()
        await limiter.acquire()  # third send must wait for the first to age out
        assert clock.sleeps == [pytest.approx(50.0)]

    async def test_minute_window_boundary_exactly_60s_is_free(self):
        clock = FakeClock()
        limiter = make_limiter(clock, max_per_minute=1)
        await limiter.acquire()
        clock.time += 60
        await limiter.acquire()
        assert clock.sleeps == []

    async def test_hour_window_blocks(self):
        clock = FakeClock()
        limiter = make_limiter(clock, max_per_hour=2)
        await limiter.acquire()
        clock.time += 100
        await limiter.acquire()
        await limiter.acquire()
        assert clock.sleeps == [pytest.approx(3500.0)]

    async def test_raise_mode_raises_instead_of_waiting(self):
        clock = FakeClock()
        limiter = make_limiter(clock, min_interval_seconds=3, on_limit="raise")
        await limiter.acquire()
        with pytest.raises(RateLimitExceededError, match="retry in"):
            await limiter.acquire()
        assert clock.sleeps == []

    async def test_daily_cap_always_raises_even_in_wait_mode(self):
        clock = FakeClock()
        limiter = make_limiter(clock, max_per_day=2)
        await limiter.acquire()
        await limiter.acquire()
        with pytest.raises(SafetyViolationError, match="Daily"):
            await limiter.acquire()
        assert clock.sleeps == []

    async def test_daily_window_slides(self):
        clock = FakeClock()
        limiter = make_limiter(clock, max_per_day=1)
        await limiter.acquire()
        clock.time += 86_399
        with pytest.raises(SafetyViolationError):
            await limiter.acquire()
        clock.time += 1  # exactly 24h after the first send
        await limiter.acquire()

    async def test_history_older_than_a_day_is_ignored(self):
        clock = FakeClock()
        history = InMemorySendHistory()
        history.add(clock.time - 90_000)
        history.add(clock.time - 5)
        limiter = RateLimiter(
            RateLimitConfig(min_interval_seconds=0, max_per_day=2), clock.as_clock(), history
        )
        await limiter.acquire()
        assert len(history.times()) == 3
        with pytest.raises(SafetyViolationError):
            await limiter.acquire()

    async def test_backwards_clock_jump_waits_instead_of_bypassing_limits(self):
        clock = FakeClock()
        limiter = make_limiter(clock, min_interval_seconds=3)
        await limiter.acquire()
        clock.time -= 1000  # wall clock jumps back an hour-ish
        await limiter.acquire()
        # The limiter treated "now" as the last send time and waited the interval.
        assert clock.sleeps and clock.sleeps[0] == pytest.approx(3.0)
        assert min(limiter._history.times()) <= max(limiter._history.times())

    def test_default_clock_is_created(self):
        assert RateLimiter(RateLimitConfig())._clock.now() > 0


class TestTypingDelay:
    def test_disabled_returns_zero(self):
        delay = TypingDelay(TypingConfig(min_delay_ms=0, max_delay_ms=0))
        assert delay.delay_ms_for("hello") == 0.0

    def test_jitter_stays_within_bounds(self):
        delay = TypingDelay(TypingConfig(min_delay_ms=30, max_delay_ms=80), random.Random(7))
        values = {delay.delay_ms_for("hello") for _ in range(200)}
        assert all(30 <= value <= 80 for value in values)
        assert len(values) > 1

    def test_injected_rng_is_used(self):
        class FixedRng(random.Random):
            def uniform(self, a, b):
                return b

        delay = TypingDelay(TypingConfig(min_delay_ms=30, max_delay_ms=80), FixedRng())
        assert delay.delay_ms_for("hi") == 80

    def test_total_time_cap_lowers_per_char_delay(self):
        config = TypingConfig(min_delay_ms=50, max_delay_ms=50, max_total_seconds=2)
        delay = TypingDelay(config)
        assert delay.delay_ms_for("x" * 100) == pytest.approx(20.0)  # 2000ms / 100 chars

    def test_cap_does_not_raise_delay_for_short_text(self):
        config = TypingConfig(min_delay_ms=50, max_delay_ms=50, max_total_seconds=10)
        assert TypingDelay(config).delay_ms_for("hi") == 50

    def test_empty_text_does_not_divide_by_zero(self):
        assert TypingDelay(TypingConfig(min_delay_ms=50, max_delay_ms=50)).delay_ms_for("") == 50
