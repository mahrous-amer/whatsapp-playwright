"""Regression tests for the code-review findings (serialization, guards, drafts...)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright.async_api import Error as PlaywrightError

import whatsapp_web
from tests.conftest import make_config, rendered
from tests.test_actions import incoming
from tests.test_lifecycle import make_playwright_mocks
from whatsapp_web import (
    AccountAtRiskError,
    BrowserLaunchError,
    ContactNotFoundError,
    GroupCreationError,
    InvalidPhoneNumberError,
    MessageSendError,
    RateLimitConfig,
    RateLimitExceededError,
    SafetyConfig,
    SafetyViolationError,
    WhatsAppWeb,
)

BAN_TEXT = "Your account is temporarily banned"


def typed_texts(env):
    return [e[1] for e in env.fake.events if e[0] in ("insert_text", "type")]


def show_banner(env, text=BAN_TEXT, selector="risk_banner"):
    banner = env.fake.add(selector)
    banner.inner_text = AsyncMock(return_value=text)
    return banner


def add_group_ui(env, participants=("Alice", "Bob"), group="Team"):
    for name in ("new_chat_button", "new_group_item", "group_participant_search",
                 "group_next_button", "group_name_input", "group_create_button"):
        env.fake.add(name)
    for participant in participants:
        env.fake.add(rendered("participant_result_templates", participant))
    env.fake.add(rendered("chat_header_title_templates", group))


class TestSerialization:
    async def test_concurrent_sends_go_to_their_own_chats_in_order(self, env):
        env.fake.add_contact("Bob")
        await asyncio.gather(
            env.wa.send_message("Alice", "for alice"),
            env.wa.send_message("Bob", "for bob"),
        )
        relevant = [e for e in env.fake.events if e[0] in ("open", "insert_text", "send")]
        assert relevant == [
            ("open", "Alice"), ("insert_text", "for alice"), ("send",),
            ("open", "Bob"), ("insert_text", "for bob"), ("send",),
        ]

    async def test_rate_limit_wait_does_not_let_another_call_interleave(self, make_env):
        env = make_env(rate_limit=RateLimitConfig(min_interval_seconds=60))
        env.fake.add_contact("Bob")
        await env.wa.send_message("Alice", "first")
        env.fake.events.clear()
        await asyncio.gather(
            env.wa.send_message("Alice", "second"),
            env.wa.send_message("Bob", "third"),
        )
        # the long wait happens before any chat is opened
        assert env.fake.events[0] == ("open", "Alice")
        assert [e for e in env.fake.events if e[0] == "send"] == [("send",), ("send",)]
        assert env.clock.sleeps == [pytest.approx(60.0), pytest.approx(60.0)]

    async def test_lock_is_released_after_an_error(self, env):
        with pytest.raises(ContactNotFoundError):
            await env.wa.send_message("Zed", "hi")
        await asyncio.wait_for(env.wa.send_message("Alice", "hi"), timeout=2)

    async def test_reads_are_serialized_with_sends(self, env):
        order = []
        original = env.wa._open_chat

        async def spy(name):
            order.append(("start", name))
            await original(name)
            order.append(("end", name))

        env.wa._open_chat = spy
        env.fake.add_contact("Bob")
        await asyncio.gather(env.wa.get_last_messages("Alice"), env.wa.is_online("Bob"))
        assert order == [("start", "Alice"), ("end", "Alice"), ("start", "Bob"), ("end", "Bob")]

    async def test_public_docstrings_survive_the_decorator(self):
        assert "Search for a contact" in WhatsAppWeb.send_message.__doc__


class TestRateSlotBeforeOpeningChat:
    async def test_slot_is_taken_before_chat_opens(self, make_env):
        env = make_env(rate_limit=RateLimitConfig(min_interval_seconds=60))
        await env.wa.send_message("Alice", "first")
        original_sleep = env.clock.sleep
        seen_events = []

        async def spying_sleep(seconds):
            seen_events.append(list(env.fake.events))
            await original_sleep(seconds)

        env.wa._controls.rate_limiter._clock = whatsapp_web.Clock(env.clock.now, spying_sleep)
        env.fake.events.clear()
        await env.wa.send_message("Alice", "second")
        assert seen_events == [[]]  # nothing happened in the page while waiting

    async def test_checks_run_again_after_the_wait(self, make_env):
        env = make_env(rate_limit=RateLimitConfig(min_interval_seconds=60))
        await env.wa.send_message("Alice", "first")
        original_sleep = env.clock.sleep

        async def sleep_then_opt_out(seconds):
            await original_sleep(seconds)
            env.wa.opt_out("Alice")  # something changed during the wait

        env.wa._controls.rate_limiter._clock = whatsapp_web.Clock(env.clock.now, sleep_then_opt_out)
        with pytest.raises(SafetyViolationError, match="opted out"):
            await env.wa.send_message("Alice", "second")
        assert typed_texts(env) == ["first"]

    async def test_wait_mode_cannot_overshoot_a_cap_via_concurrent_calls(self, make_env):
        env = make_env(safety=SafetyConfig(max_new_recipients_per_day=1))
        env.fake.add_contact("Bob")
        results = await asyncio.gather(
            env.wa.send_message("Alice", "x"),
            env.wa.send_message("Bob", "y"),
            return_exceptions=True,
        )
        assert results[0] is None
        assert isinstance(results[1], SafetyViolationError)


class TestRiskWarnings:
    async def test_ban_dialog_on_number_path_trips_breaker_and_is_not_dismissed(self, env):
        env.fake.remove("message_composer")
        env.fake.add("modal_dialog")
        button = env.fake.add("modal_button")
        show_banner(env)
        with pytest.raises(AccountAtRiskError, match="temporarily banned"):
            await env.wa.send_message_to_number("+201001234567", "hi")
        button.click.assert_not_awaited()
        with pytest.raises(AccountAtRiskError):
            env.guard.ensure_breaker_not_tripped()

    async def test_plain_invalid_number_dialog_is_still_dismissed(self, env):
        env.fake.remove("message_composer")
        env.fake.add("modal_dialog")
        button = env.fake.add("modal_button")
        show_banner(env, "Phone number shared via url is invalid.")
        with pytest.raises(InvalidPhoneNumberError):
            await env.wa.send_message_to_number("+201001234567", "hi")
        button.click.assert_awaited_once()

    async def test_opening_a_chat_for_reading_also_checks_for_warnings(self, env):
        show_banner(env)
        with pytest.raises(AccountAtRiskError):
            await env.wa.get_last_messages("Alice")

    async def test_reads_are_allowed_while_the_breaker_is_tripped(self, env):
        env.guard.trip_breaker("manual")
        assert await env.wa.get_last_messages("Alice") == []

    async def test_warning_that_appears_after_a_successful_send_trips_breaker(self, env):
        show_banner(env).is_visible = AsyncMock(side_effect=lambda: ("send",) in env.fake.events)
        await env.wa.send_message("Alice", "hi")  # delivered, so no error for the caller
        with pytest.raises(AccountAtRiskError):
            await env.wa.send_message("Alice", "again")

    async def test_warning_after_a_failed_send_raises_account_at_risk(self, env):
        attempted = []

        def click_without_sending():
            attempted.append(True)

        env.fake.on_send = click_without_sending
        show_banner(env).is_visible = AsyncMock(side_effect=lambda: bool(attempted))
        with pytest.raises(AccountAtRiskError, match="temporarily banned"):
            await env.wa.send_message("Alice", "hi")

    async def test_page_errors_while_scanning_do_not_mask_the_send_failure(self, env):
        attempted = []
        env.fake.on_send = lambda: attempted.append(True)

        async def explode_after_click():
            if attempted:
                raise PlaywrightError("page closed")
            return False

        show_banner(env).is_visible = explode_after_click
        with pytest.raises(MessageSendError):
            await env.wa.send_message("Alice", "hi")

    async def test_page_errors_while_scanning_after_success_are_ignored(self, env):
        async def explode_after_send():
            if ("send",) in env.fake.events:
                raise PlaywrightError("page closed")
            return False

        show_banner(env).is_visible = explode_after_send
        await env.wa.send_message("Alice", "hi")


class TestCreateGroupIsGuarded:
    async def test_happy_path_uses_a_slot_and_counts_participants_as_new(self, env):
        add_group_ui(env)
        await env.wa.create_group("Team", ["Alice", "Bob"])
        state = env.guard._store.state
        assert len(env.guard.send_times) == 1
        assert {key for _, key in state.new_recipients} == {"name:alice", "name:bob"}
        assert len(state.group_creations) == 1

    async def test_tripped_breaker_blocks_before_any_click(self, env):
        add_group_ui(env)
        env.guard.trip_breaker("manual")
        with pytest.raises(AccountAtRiskError):
            await env.wa.create_group("Team", ["Alice"])
        env.fake.registry["new_chat_button"].click.assert_not_awaited()

    async def test_risk_banner_blocks_group_creation(self, env):
        add_group_ui(env)
        show_banner(env)
        with pytest.raises(AccountAtRiskError):
            await env.wa.create_group("Team", ["Alice"])

    async def test_rate_limit_applies(self, make_env):
        env = make_env(rate_limit=RateLimitConfig(min_interval_seconds=60, on_limit="raise"))
        add_group_ui(env)
        await env.wa.send_message("Alice", "hi")
        with pytest.raises(RateLimitExceededError):
            await env.wa.create_group("Team", ["Alice"])

    async def test_opted_out_participant_blocks(self, env):
        add_group_ui(env)
        env.wa.opt_out("Bob")
        with pytest.raises(SafetyViolationError, match="opted out"):
            await env.wa.create_group("Team", ["Alice", "Bob"])

    async def test_participants_count_towards_new_recipient_cap(self, make_env):
        env = make_env(safety=SafetyConfig(max_new_recipients_per_day=1))
        add_group_ui(env)
        with pytest.raises(SafetyViolationError, match="New-recipient"):
            await env.wa.create_group("Team", ["Alice", "Bob"])
        env.fake.registry["new_group_item"].click.assert_awaited_once()  # dialog opened, nothing picked

    async def test_daily_group_cap(self, make_env):
        env = make_env(safety=SafetyConfig(max_groups_per_day=1))
        add_group_ui(env)
        await env.wa.create_group("Team", ["Alice"])
        with pytest.raises(SafetyViolationError, match="Group-creation"):
            await env.wa.create_group("Team", ["Alice"])

    async def test_repeated_failures_trip_the_breaker(self, make_env):
        env = make_env(safety=SafetyConfig(max_groups_per_day=10, max_consecutive_failures=2))
        add_group_ui(env)
        env.fake.remove(rendered("chat_header_title_templates", "Team"))
        for _ in range(2):
            with pytest.raises(GroupCreationError):
                await env.wa.create_group("Team", ["Alice"])
        with pytest.raises(AccountAtRiskError, match="2 consecutive"):
            await env.wa.create_group("Team", ["Alice"])

    async def test_unknown_participant_is_not_a_breaker_failure(self, make_env):
        env = make_env(safety=SafetyConfig(max_groups_per_day=10))
        add_group_ui(env)
        for _ in range(4):
            with pytest.raises(ContactNotFoundError):
                await env.wa.create_group("Team", ["Zed"])
        env.guard.ensure_breaker_not_tripped()


class TestOptOutMatching:
    @pytest.mark.parametrize("text", ["STOP.", "Stop!", "stop 🙏", "unsubscribe me please"])
    async def test_punctuated_and_phrased_requests_block_sends(self, env, monkeypatch, text):
        monkeypatch.setattr(env.wa, "_read_recent_messages", AsyncMock(return_value=[incoming("false_1", text)]))
        with pytest.raises(SafetyViolationError, match="opted out"):
            await env.wa.send_message("Alice", "hi")
        assert typed_texts(env) == []


class TestIdentityLinking:
    async def test_number_chat_titled_with_a_saved_name_shares_opt_out(self, env):
        env.fake.add("chat_header_title").inner_text = AsyncMock(return_value="Alice")
        env.wa.opt_out("Alice")
        with pytest.raises(SafetyViolationError, match="opted out"):
            await env.wa.send_message_to_number("+20 100 123 4567", "hi")
        assert typed_texts(env) == []
        assert env.wa.is_opted_out("+201001234567")

    async def test_opt_out_by_number_blocks_name_sends_after_linking(self, env):
        env.fake.add("chat_header_title").inner_text = AsyncMock(return_value="Alice")
        env.wa.opt_in("Alice")
        await env.wa.send_message_to_number("+201001234567", "hi")
        env.wa.opt_out("+201001234567")
        with pytest.raises(SafetyViolationError):
            await env.wa.send_message("Alice", "hi again")

    async def test_linked_identity_is_not_double_counted(self, make_env):
        env = make_env(safety=SafetyConfig(max_new_recipients_per_day=1))
        env.fake.add("chat_header_title").inner_text = AsyncMock(return_value="Alice")
        await env.wa.send_message("Alice", "one")
        await env.wa.send_message_to_number("+201001234567", "two")
        assert typed_texts(env) == ["one", "two"]
        assert len(env.guard._store.state.new_recipients) == 1

    @pytest.mark.parametrize("title", ["", "+20 100 123 4567"])
    async def test_untitled_or_number_titled_chats_are_not_linked(self, env, title):
        env.fake.add("chat_header_title").inner_text = AsyncMock(return_value=title)
        await env.wa.send_message_to_number("+201001234567", "hi")
        assert env.guard._store.state.identity_links == {}


class TestPersistedSafetyOnClient:
    async def test_corrupt_state_file_keeps_default_client_halted(self, tmp_path):
        profile = tmp_path / "profile"
        profile.mkdir()
        (profile / "safety_state.json").write_text("{garbage")
        wa = WhatsAppWeb(make_config(tmp_path))
        fake_env_page = MagicMock()
        fake_env_page.locator = MagicMock(return_value=MagicMock(first=MagicMock(
            is_visible=AsyncMock(return_value=False))))
        wa._page = fake_env_page
        with pytest.raises(AccountAtRiskError, match="unreadable safety state"):
            await wa.send_message("Alice", "hi")
        assert list(profile.glob("safety_state.json.corrupt-*"))
        assert json.loads((profile / "safety_state.json").read_text())["breaker_reason"]


class TestComposerAndConfirmation:
    async def test_existing_draft_is_cleared_before_typing(self, env):
        await env.wa.send_message("Alice", "Hello")
        events = env.fake.events
        select_all = events.index(("press", "ControlOrMeta+A"))
        backspace = events.index(("press", "Backspace"))
        typing = events.index(("insert_text", "Hello"))
        assert select_all < backspace < typing

    async def test_count_change_with_same_id_is_not_treated_as_sent(self, env):
        env.fake.on_send = lambda: None
        counter = iter(range(1, 1000))
        env.fake.registry["outgoing_messages"].count = AsyncMock(side_effect=lambda: next(counter))
        with pytest.raises(MessageSendError):
            await env.wa.send_message("Alice", "hi")

    async def test_missing_baseline_id_falls_back_to_count(self, env):
        env.fake.outgoing.ids.clear()
        env.fake.outgoing.ids.append(None)
        env.fake.registry["outgoing_messages"].get_attribute = AsyncMock(
            side_effect=lambda name: env.fake.outgoing.ids[-1]
        )
        await env.wa.send_message("Alice", "hi")  # id None -> "true_1": a different message


class TestStartCleanup:
    @pytest.fixture
    def client(self, tmp_path):
        return WhatsAppWeb(make_config(tmp_path))

    async def test_failure_after_launch_closes_everything(self, client):
        factory, playwright, context, page = make_playwright_mocks()
        page.set_default_timeout.side_effect = RuntimeError("bad page")
        with patch.object(whatsapp_web, "async_playwright", factory):
            with pytest.raises(RuntimeError, match="bad page"):
                await client.start()
        context.close.assert_awaited_once()
        playwright.stop.assert_awaited_once()
        assert client._context is None and client._page is None

    async def test_cancellation_during_setup_also_cleans_up(self, client):
        factory, playwright, context, _ = make_playwright_mocks(pages=[])
        context.new_page.side_effect = asyncio.CancelledError()
        with patch.object(whatsapp_web, "async_playwright", factory):
            with pytest.raises(asyncio.CancelledError):
                await client.start()
        context.close.assert_awaited_once()
        playwright.stop.assert_awaited_once()

    async def test_launch_failure_still_wraps_the_error(self, client):
        factory, playwright, *_ = make_playwright_mocks()
        playwright.chromium.launch_persistent_context.side_effect = RuntimeError("x")
        with patch.object(whatsapp_web, "async_playwright", factory):
            with pytest.raises(BrowserLaunchError):
                await client.start()
        playwright.stop.assert_awaited_once()
