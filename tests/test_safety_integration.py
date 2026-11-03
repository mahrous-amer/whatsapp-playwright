"""Guardrails as enforced by WhatsAppWeb's send paths."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from tests.test_actions import incoming
from whatsapp_web import (
    AccountAtRiskError,
    ContactNotFoundError,
    ChatMessage,
    ElementNotFoundError,
    MessageDirection,
    MessageSendError,
    RateLimitConfig,
    RateLimitExceededError,
    SafetyConfig,
    SafetyStore,
    SafetyViolationError,
)


def feed_history(env, monkeypatch, messages):
    monkeypatch.setattr(env.wa, "_read_recent_messages", AsyncMock(return_value=messages))


def show_banner(env, text):
    banner = env.fake.add("risk_banner")
    banner.inner_text = AsyncMock(return_value=text)
    return banner


def typed_texts(env):
    return [e[1] for e in env.fake.events if e[0] in ("insert_text", "type")]


def add_file_ui(env):
    for name in ("attach_button", "media_send_button", "document_file_input", "caption_input"):
        env.fake.add(name)


class TestRateLimitIntegration:
    async def test_every_send_path_takes_a_rate_limit_slot(self, make_env, tmp_path):
        env = make_env()
        add_file_ui(env)
        document = tmp_path / "d.pdf"
        document.write_bytes(b"x")
        await env.wa.send_message("Alice", "one")
        await env.wa.send_message_to_number("+201001234567", "two")
        await env.wa.send_file("Alice", document)
        assert len(env.guard.send_times) == 3

    async def test_raise_mode_blocks_second_send(self, make_env):
        env = make_env(rate_limit=RateLimitConfig(min_interval_seconds=60, on_limit="raise"))
        await env.wa.send_message("Alice", "one")
        with pytest.raises(RateLimitExceededError):
            await env.wa.send_message("Alice", "two")

    async def test_wait_mode_sleeps_instead(self, make_env):
        env = make_env(rate_limit=RateLimitConfig(min_interval_seconds=60))
        await env.wa.send_message("Alice", "one")
        await env.wa.send_message("Alice", "two")
        assert env.clock.sleeps == [pytest.approx(60.0)]

    async def test_daily_cap(self, make_env):
        env = make_env(rate_limit=RateLimitConfig(min_interval_seconds=0, max_per_day=1))
        await env.wa.send_message("Alice", "one")
        with pytest.raises(SafetyViolationError, match="Daily"):
            await env.wa.send_message("Alice", "two")


class TestOptOutIntegration:
    async def test_stop_in_history_blocks_send(self, env, monkeypatch):
        feed_history(env, monkeypatch, [incoming("false_1", "STOP")])
        with pytest.raises(SafetyViolationError, match="opted out"):
            await env.wa.send_message("Alice", "hi")
        assert env.wa.is_opted_out("Alice")
        env.fake.page.keyboard.insert_text.assert_not_awaited()

    async def test_get_last_messages_records_opt_out(self, env, monkeypatch):
        feed_history(env, monkeypatch, [incoming("false_1", "unsubscribe")])
        await env.wa.get_last_messages("Alice")
        assert env.wa.is_opted_out("Alice")

    async def test_wait_for_message_records_opt_out(self, env, monkeypatch):
        reads = AsyncMock(side_effect=[[], [incoming("false_5", "Stop")]])
        monkeypatch.setattr(env.wa, "_read_recent_messages", reads)
        await env.wa.wait_for_message("Alice", timeout_s=5)
        assert env.wa.is_opted_out("Alice")

    async def test_outgoing_stop_text_is_ignored(self, env, monkeypatch):
        mine = ChatMessage("stop", MessageDirection.OUTGOING, message_id="true_1")
        feed_history(env, monkeypatch, [mine])
        await env.wa.send_message("Alice", "hi")
        assert not env.wa.is_opted_out("Alice")

    async def test_manual_opt_out_blocks_and_opt_in_allows(self, env):
        env.wa.opt_out("Alice")
        with pytest.raises(SafetyViolationError):
            await env.wa.send_message("Alice", "hi")
        assert typed_texts(env) == []
        env.wa.opt_in("Alice")
        await env.wa.send_message("Alice", "hi")
        assert typed_texts(env) == ["hi"]
        assert len(env.guard.send_times) == 1

    async def test_number_opt_out_applies_to_number_sends(self, env):
        env.wa.opt_out("+20 100 123 4567")
        with pytest.raises(SafetyViolationError):
            await env.wa.send_message_to_number("201001234567", "hi")

    async def test_file_send_respects_opt_out(self, env, tmp_path):
        document = tmp_path / "d.pdf"
        document.write_bytes(b"x")
        env.wa.opt_out("Alice")
        with pytest.raises(SafetyViolationError):
            await env.wa.send_file("Alice", document)


class TestCapsIntegration:
    async def test_new_recipient_cap_for_names_and_numbers(self, make_env):
        env = make_env(safety=SafetyConfig(max_new_recipients_per_day=2))
        env.fake.add_contact("Cy")
        await env.wa.send_message("Alice", "a")
        await env.wa.send_message_to_number("+201001234567", "b")
        with pytest.raises(SafetyViolationError, match="New-recipient"):
            await env.wa.send_message("Cy", "c")
        await env.wa.send_message("Alice", "again")
        assert typed_texts(env) == ["a", "b", "again"]
        # 3 sends + the refused one: its slot is taken before the chat is opened
        assert len(env.guard.send_times) == 4

    async def test_recipient_who_wrote_first_is_not_new(self, make_env, monkeypatch):
        env = make_env(safety=SafetyConfig(max_new_recipients_per_day=1))
        env.fake.add_contact("Bob")
        await env.wa.send_message("Alice", "a")
        feed_history(env, monkeypatch, [incoming("false_1", "hello!")])
        await env.wa.send_message("Bob", "reply")
        assert typed_texts(env) == ["a", "reply"]
        assert len(env.guard.send_times) == 2

    async def test_identical_message_cap(self, make_env):
        env = make_env(safety=SafetyConfig(max_identical_recipients=1))
        env.fake.add_contact("Bob")
        await env.wa.send_message("Alice", "Big   Sale")
        with pytest.raises(SafetyViolationError, match="Identical"):
            await env.wa.send_message("Bob", "big sale")
        await env.wa.send_message("Alice", "big sale")

    async def test_file_caption_counts_towards_identical_cap(self, make_env, tmp_path):
        env = make_env(safety=SafetyConfig(max_identical_recipients=1))
        env.fake.add_contact("Bob")
        add_file_ui(env)
        document = tmp_path / "d.pdf"
        document.write_bytes(b"x")
        other = tmp_path / "other.pdf"
        other.write_bytes(b"y")
        await env.wa.send_file("Alice", document, caption="Promo")
        with pytest.raises(SafetyViolationError, match="Identical"):
            await env.wa.send_file("Bob", document, caption="promo")
        await env.wa.send_file("Bob", document, caption="different caption")
        await env.wa.send_file("Bob", other, caption="Promo")  # other content

    async def test_caption_less_file_is_also_deduplicated(self, make_env, tmp_path):
        env = make_env(safety=SafetyConfig(max_identical_recipients=1))
        env.fake.add_contact("Bob")
        add_file_ui(env)
        document = tmp_path / "d.pdf"
        document.write_bytes(b"x")
        await env.wa.send_file("Alice", document)
        with pytest.raises(SafetyViolationError, match="Identical"):
            await env.wa.send_file("Bob", document)


class TestBreakerIntegration:
    async def test_warning_banner_trips_breaker_until_reset(self, env, caplog):
        banner = show_banner(env, "Your account is temporarily banned  for spam")
        with caplog.at_level(logging.WARNING):
            with pytest.raises(AccountAtRiskError, match="temporarily banned"):
                await env.wa.send_message("Alice", "hi")
        assert "breaker tripped" in caplog.text
        banner.is_visible = AsyncMock(return_value=False)
        with pytest.raises(AccountAtRiskError, match="reset_safety_breaker"):
            await env.wa.send_message("Alice", "hi")
        env.wa.reset_safety_breaker()
        await env.wa.send_message("Alice", "hi")

    async def test_unrelated_dialog_is_not_a_warning(self, env):
        show_banner(env, "Update available")
        await env.wa.send_message("Alice", "hi")
        assert typed_texts(env) == ["hi"]
        assert len(env.guard.send_times) == 1

    async def test_unexpected_logout_trips_breaker(self, env):
        await env.wa.login()
        env.fake.add("qr_code")
        with pytest.raises(AccountAtRiskError, match="logged out unexpectedly"):
            await env.wa.send_message("Alice", "hi")
        env.fake.remove("qr_code")
        with pytest.raises(AccountAtRiskError):
            await env.wa.send_message("Alice", "hi")

    async def test_consecutive_send_failures_trip_breaker(self, env):
        env.fake.on_send = lambda: None
        for _ in range(3):
            with pytest.raises(MessageSendError):
                await env.wa.send_message("Alice", "hi")
        with pytest.raises(AccountAtRiskError, match="3 consecutive"):
            await env.wa.send_message("Alice", "hi")

    async def test_success_between_failures_resets_streak(self, env):
        working_send = env.fake.on_send
        for _ in range(2):
            env.fake.on_send = lambda: None
            for _ in range(2):
                with pytest.raises(MessageSendError):
                    await env.wa.send_message("Alice", "hi")
            env.fake.on_send = working_send
            await env.wa.send_message("Alice", "hi")
        assert typed_texts(env).count("hi") == 6

    async def test_missing_send_element_counts_as_failure(self, make_env):
        env = make_env(safety=SafetyConfig(max_consecutive_failures=1))
        # First lookup (opening the chat) works; the lookup inside the send fails.
        calls = iter(range(100))

        async def fails_from_second_lookup(**_kwargs):
            if next(calls) >= 1:
                raise PlaywrightTimeoutError("gone")

        env.fake.registry["message_composer"].wait_for = fails_from_second_lookup
        with pytest.raises(ElementNotFoundError):
            await env.wa.send_message("Alice", "hi")
        env.fake.registry["message_composer"].wait_for = AsyncMock()
        with pytest.raises(AccountAtRiskError):
            await env.wa.send_message("Alice", "hi")

    async def test_unknown_contact_does_not_count_as_failure(self, env):
        for _ in range(5):
            with pytest.raises(ContactNotFoundError):
                await env.wa.send_message("Zed", "hi")
        await env.wa.send_message("Alice", "hi")


async def test_state_persists_across_restarts(make_env):
    env = make_env()
    await env.wa.send_message("Alice", "hi")
    env.wa.opt_out("Bob")
    reloaded = SafetyStore(env.state_path).state
    assert "name:bob" in reloaded.opted_out
    assert len(reloaded.send_times) == 1
    assert len(reloaded.broadcasts) == 1
