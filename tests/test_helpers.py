"""Pure helpers, dataclass validation and the retry helper."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Error as PlaywrightError

from whatsapp_web import (
    ChatMessage,
    InvalidPhoneNumberError,
    MessageDirection,
    RateLimitConfig,
    RetryPolicy,
    SafetyConfig,
    Selectors,
    TypingConfig,
    WhatsAppWebConfig,
    build_chat_message,
    build_unread_chat,
    normalize_phone_number,
    parse_pre_plain_text,
    quote_for_selector,
    recipient_key,
    render_selector_templates,
    retry_async,
    _Deadline,
    _message_key,
    _require_non_blank,
)


class TestNormalizePhoneNumber:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("+20 100 123 4567", "201001234567"),
            ("0020-100-123-4567", "201001234567"),
            ("(1) 555.123.4567", "15551234567"),
            ("201001234567", "201001234567"),
        ],
    )
    def test_accepts_common_formats(self, raw, expected):
        assert normalize_phone_number(raw) == expected

    @pytest.mark.parametrize("raw", ["", "abc", "+123", "+1234567890123456", "12345x7890"])
    def test_rejects_malformed_numbers(self, raw):
        with pytest.raises(InvalidPhoneNumberError):
            normalize_phone_number(raw)

    def test_rejects_national_format_without_country_code(self):
        with pytest.raises(InvalidPhoneNumberError, match="country code"):
            normalize_phone_number("0100 123 4567")


def test_quote_and_render_templates_escape_quotes_and_keep_unicode():
    assert quote_for_selector('Ann "A"') == '"Ann \\"A\\""'
    assert quote_for_selector("أحمد") == '"أحمد"'
    assert render_selector_templates(("a[{name}]", "b[{name}]"), "x") == ('a["x"]', 'b["x"]')


class TestParsePrePlainText:
    @pytest.mark.parametrize("value", [None, "", "garbage"])
    def test_unparseable_returns_nothing(self, value):
        assert parse_pre_plain_text(value) == (None, None)

    def test_24h_us_date(self):
        stamp, sender = parse_pre_plain_text("[10:30, 11/13/2026] Alice: ")
        assert stamp == datetime(2026, 11, 13, 10, 30)
        assert sender == "Alice"

    def test_12h_with_narrow_nbsp(self):
        stamp, _ = parse_pre_plain_text("[3:05 PM, 11/13/2026] Bob: ")
        assert stamp == datetime(2026, 11, 13, 15, 5)

    def test_day_first_when_month_is_impossible(self):
        stamp, _ = parse_pre_plain_text("[10:30, 25/12/2026] Alice: ")
        assert stamp == datetime(2026, 12, 25, 10, 30)

    def test_unknown_date_format_keeps_sender(self):
        assert parse_pre_plain_text("[10:30, someday] Alice: ") == (None, "Alice")

    def test_empty_sender_becomes_none(self):
        stamp, sender = parse_pre_plain_text("[10:30, 2026-11-13] : ")
        assert stamp == datetime(2026, 11, 13, 10, 30)
        assert sender is None


class TestBuildChatMessage:
    def test_outgoing_by_id_prefix(self):
        message = build_chat_message({"dataId": "true_1", "text": " hi ", "className": ""})
        assert message.direction is MessageDirection.OUTGOING
        assert message.text == "hi"
        assert message.message_id == "true_1"

    def test_incoming_by_id_prefix(self):
        message = build_chat_message({"dataId": "false_9", "text": "yo"})
        assert message.direction is MessageDirection.INCOMING

    def test_falls_back_to_css_class(self):
        out = build_chat_message({"dataId": None, "className": "x message-out"})
        incoming = build_chat_message({"dataId": None, "className": "message-in"})
        assert out.direction is MessageDirection.OUTGOING
        assert incoming.direction is MessageDirection.INCOMING
        assert out.message_id is None and out.text == ""

    def test_own_message_marker_wins_over_missing_prefix(self):
        message = build_chat_message({"dataId": "3EB0ABC", "hasOutgoingMarker": True})
        assert message.direction is MessageDirection.OUTGOING

    def test_unprefixed_id_without_marker_is_incoming(self):
        message = build_chat_message({"dataId": "3EB0ABC", "hasOutgoingMarker": False})
        assert message.direction is MessageDirection.INCOMING

    def test_timestamp_and_sender_are_parsed(self):
        message = build_chat_message(
            {"dataId": "false_1", "text": "t", "prePlainText": "[10:30, 11/13/2026] Alice: "}
        )
        assert message.sender == "Alice"
        assert message.timestamp == datetime(2026, 11, 13, 10, 30)


class TestBuildUnreadChat:
    def test_count_from_badge_text(self):
        chat = build_unread_chat({"name": "A", "hasBadge": True, "badgeText": "3"})
        assert chat is not None and chat.unread_count == 3

    def test_count_from_aria_label(self):
        chat = build_unread_chat(
            {"name": "A", "hasBadge": True, "badgeText": "", "badgeLabel": "12 unread messages"}
        )
        assert chat is not None and chat.unread_count == 12

    def test_badge_without_number_counts_as_one(self):
        chat = build_unread_chat({"name": "A", "hasBadge": True, "badgeText": None})
        assert chat is not None and chat.unread_count == 1

    @pytest.mark.parametrize(
        "raw", [{"name": "A", "hasBadge": False}, {"name": "", "hasBadge": True}, {"hasBadge": True}]
    )
    def test_read_or_unnamed_chats_are_skipped(self, raw):
        assert build_unread_chat(raw) is None


def test_message_key_prefers_id_then_content():
    with_id = ChatMessage("t", MessageDirection.INCOMING, message_id="false_1")
    without_id = ChatMessage("t", MessageDirection.INCOMING)
    assert _message_key(with_id) == ("id", "false_1")
    assert _message_key(without_id) == ("content", "t", "None")


def test_recipient_key_maps_numbers_and_names():
    assert recipient_key("+20 100 123 4567") == "phone:201001234567"
    assert recipient_key("201001234567") == "phone:201001234567"
    assert recipient_key("  Alice  Smith ") == "name:alice smith"
    assert recipient_key("0100 123") == "name:0100 123"  # looks numeric but invalid


def test_require_non_blank():
    _require_non_blank("x", "field")
    for bad in ("", "   "):
        with pytest.raises(ValueError, match="field"):
            _require_non_blank(bad, "field")


async def test_deadline_expires():
    assert _Deadline(0).is_expired
    assert not _Deadline(60).is_expired


class TestRetryAsync:
    async def test_returns_first_success_without_sleeping(self):
        operation = AsyncMock(return_value=5)
        assert await retry_async(operation, RetryPolicy(3, 0)) == 5
        assert operation.await_count == 1

    async def test_retries_then_succeeds(self):
        operation = AsyncMock(side_effect=[ValueError("a"), ValueError("b"), "ok"])
        assert await retry_async(operation, RetryPolicy(3, 0, (ValueError,))) == "ok"

    async def test_final_error_propagates(self):
        operation = AsyncMock(side_effect=ValueError("always"))
        with pytest.raises(ValueError, match="always"):
            await retry_async(operation, RetryPolicy(3, 0, (ValueError,)))
        assert operation.await_count == 3

    async def test_unlisted_errors_are_not_retried(self):
        operation = AsyncMock(side_effect=KeyError("k"))
        with pytest.raises(KeyError):
            await retry_async(operation, RetryPolicy(3, 0, (ValueError,)))
        assert operation.await_count == 1

    async def test_single_attempt_runs_once(self):
        operation = AsyncMock(side_effect=PlaywrightError("x"))
        with pytest.raises(PlaywrightError):
            await retry_async(operation, RetryPolicy(1, 0))
        assert operation.await_count == 1


class TestConfigValidation:
    def test_defaults_are_valid_and_path_is_normalised(self):
        config = WhatsAppWebConfig(user_data_dir="~/x")
        assert config.user_data_dir.name == "x" and "~" not in str(config.user_data_dir)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"retry_attempts": 0},
            {"default_timeout_ms": 0},
            {"short_timeout_ms": -1},
            {"poll_interval_s": -1},
            {"retry_delay_s": -1},
        ],
    )
    def test_invalid_client_config(self, kwargs):
        with pytest.raises(ValueError):
            WhatsAppWebConfig(**kwargs)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"min_interval_seconds": -1},
            {"max_per_minute": 0},
            {"max_per_hour": 0},
            {"max_per_day": 0},
            {"on_limit": "explode"},
        ],
    )
    def test_invalid_rate_limit_config(self, kwargs):
        with pytest.raises(ValueError):
            RateLimitConfig(**kwargs)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"min_delay_ms": -1},
            {"min_delay_ms": 50, "max_delay_ms": 10},
            {"max_total_seconds": 0},
        ],
    )
    def test_invalid_typing_config(self, kwargs):
        with pytest.raises(ValueError):
            TypingConfig(**kwargs)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"max_new_recipients_per_day": 0},
            {"max_identical_recipients": 0},
            {"max_consecutive_failures": 0},
        ],
    )
    def test_invalid_safety_config(self, kwargs):
        with pytest.raises(ValueError):
            SafetyConfig(**kwargs)

    def test_safety_config_normalises_keywords_and_path(self):
        config = SafetyConfig(opt_out_keywords=frozenset({" STOP ", "Un  Subscribe"}), state_file="~/s.json")
        assert config.opt_out_keywords == frozenset({"stop", "un subscribe"})
        assert config.state_file.name == "s.json"

    def test_selectors_are_immutable_and_overridable(self):
        from dataclasses import FrozenInstanceError, replace

        selectors = Selectors()
        with pytest.raises(FrozenInstanceError):
            selectors.chat_list = ("x",)  # type: ignore[misc]
        assert replace(selectors, chat_list=("x",)).chat_list == ("x",)
