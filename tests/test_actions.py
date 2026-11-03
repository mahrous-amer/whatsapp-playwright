"""Public WhatsAppWeb operations against a fake page."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from tests.conftest import changing_values, make_locator, rendered
from whatsapp_web import (
    AccountAtRiskError,
    AttachmentError,
    ChatMessage,
    ContactNotFoundError,
    ElementNotFoundError,
    GroupCreationError,
    InvalidPhoneNumberError,
    LoginTimeoutError,
    LogoutError,
    MessageDirection,
    MessageSendError,
    MessageTimeoutError,
    MessageUnconfirmedError,
    SessionExpiredError,
    TypingConfig,
)


def raw_message(message_id, text="hello", pre="[10:30, 11/13/2026] Alice: "):
    return {"dataId": message_id, "text": text, "className": "", "prePlainText": pre}


def set_rows(env, raws):
    """Make the chat show messages built from raw dicts (oldest first)."""
    rows = env.fake.add("message_rows", match_count=len(raws))
    row_locators = []
    for raw in raws:
        row = make_locator()
        row.evaluate = AsyncMock(return_value=raw)
        row_locators.append(row)
    rows.nth = lambda index: row_locators[index]
    return rows


def incoming(message_id, text="hi"):
    return ChatMessage(text, MessageDirection.INCOMING, message_id=message_id)


class TestLogin:
    async def test_succeeds_when_chat_list_is_visible(self, env):
        await env.wa.login()
        env.fake.page.goto.assert_awaited_once_with(
            "https://web.whatsapp.com", wait_until="domcontentloaded"
        )
        assert env.wa._was_logged_in

    async def test_waits_for_qr_scan_and_announces_once(self, env, caplog):
        env.fake.registry["chat_list"].is_visible = AsyncMock(side_effect=[False, False, False, True])
        env.fake.add("qr_code")
        with caplog.at_level(logging.INFO):
            await env.wa.login()
        assert caplog.text.count("QR code is displayed") == 1

    async def test_headless_qr_warns(self, make_env, caplog):
        env = make_env(headless=True)
        env.fake.registry["chat_list"].is_visible = AsyncMock(side_effect=[False, True])
        env.fake.add("qr_code")
        with caplog.at_level(logging.WARNING):
            await env.wa.login()
        assert "headless" in caplog.text

    async def test_timeout_raises_clear_error(self, env):
        env.fake.remove("chat_list")
        env.fake.add("qr_code")
        with pytest.raises(LoginTimeoutError, match="QR code scanned"):
            await env.wa.login()
        assert not env.wa._was_logged_in

    async def test_navigation_is_retried(self, env):
        env.fake.page.goto = AsyncMock(side_effect=[PlaywrightError("net"), None])
        await env.wa.login()
        assert env.fake.page.goto.await_count == 2


class TestSendMessage:
    async def test_happy_path(self, env):
        await env.wa.send_message("Alice", "Hello")
        fake = env.fake
        fake.registry["search_box"].fill.assert_any_await("Alice")
        fake.registry["search_box"].fill.assert_awaited_with("")
        fake.registry[rendered("chat_result_templates", "Alice")].click.assert_awaited_once()
        fake.page.keyboard.insert_text.assert_awaited_once_with("Hello")
        fake.registry["send_button"].click.assert_awaited_once()

    async def test_multiline_uses_shift_enter(self, env):
        await env.wa.send_message("Alice", "a\r\n\nb")
        events = env.fake.events
        typed = [e for e in events if e[0] == "insert_text"]
        line_breaks = [e for e in events if e == ("press", "Shift+Enter")]
        assert [e[1] for e in typed] == ["a", "b"]
        assert len(line_breaks) == 2

    async def test_types_with_delay_when_enabled(self, make_env):
        env = make_env(typing=TypingConfig(min_delay_ms=30, max_delay_ms=80))
        await env.wa.send_message("Alice", "line1\nline2")
        keyboard = env.fake.page.keyboard
        delays = [c.kwargs["delay"] for c in keyboard.type.await_args_list]
        assert len(delays) == 2 and all(30 <= d <= 80 for d in delays)
        keyboard.insert_text.assert_not_awaited()

    async def test_presses_enter_when_send_button_is_missing(self, env):
        env.fake.remove("send_button")
        await env.wa.send_message("Alice", "Hello")
        env.fake.registry["message_composer"].press.assert_awaited_once_with("Enter")

    @pytest.mark.parametrize("contact, message", [("", "hi"), ("Alice", "  ")])
    async def test_blank_arguments_raise_value_error(self, env, contact, message):
        with pytest.raises(ValueError):
            await env.wa.send_message(contact, message)

    async def test_unknown_contact_raises_without_retrying(self, env):
        with pytest.raises(ContactNotFoundError, match="Zed"):
            await env.wa.send_message("Zed", "hi")
        assert env.fake.registry["search_box"].click.await_count == 1

    async def test_missing_search_box_is_retried_then_raises(self, env):
        env.fake.remove("search_box")
        with pytest.raises(ElementNotFoundError):
            await env.wa.send_message("Alice", "hi")

    async def test_flaky_search_box_recovers_on_retry(self, env):
        env.fake.registry["search_box"].wait_for = AsyncMock(
            side_effect=[PlaywrightTimeoutError("slow"), None, None]
        )
        await env.wa.send_message("Alice", "hi")

    async def test_missing_composer_raises(self, env):
        env.fake.remove("message_composer")
        with pytest.raises(ElementNotFoundError):
            await env.wa.send_message("Alice", "hi")

    async def test_qr_screen_means_session_expired(self, env):
        env.fake.add("qr_code")
        with pytest.raises(SessionExpiredError):
            await env.wa.send_message("Alice", "hi")

    async def test_unconfirmed_message_raises_send_error(self, env):
        env.fake.on_send = lambda: None  # the click happens but nothing is sent
        with pytest.raises(MessageSendError, match="did not appear") as excinfo:
            await env.wa.send_message("Alice", "hi")
        assert not isinstance(excinfo.value, MessageUnconfirmedError)

    async def test_message_stuck_pending_raises(self, env):
        env.fake.outgoing.is_pending = True
        with pytest.raises(MessageUnconfirmedError, match="may have been delivered"):
            await env.wa.send_message("Alice", "hi")

    async def test_pending_clears_after_a_few_polls(self, env):
        outgoing = env.fake.registry["outgoing_messages"]
        outgoing.locator.return_value.count = AsyncMock(side_effect=changing_values(0, 1, 1, 0))
        await env.wa.send_message("Alice", "hi")

    async def test_first_ever_message_in_empty_chat_is_confirmed(self, env):
        env.fake.outgoing.ids.clear()
        await env.wa.send_message("Alice", "first")
        assert env.fake.outgoing.ids == ["true_0"]


class TestSendMessageToNumber:
    async def test_happy_path_navigates_to_send_url(self, env):
        await env.wa.send_message_to_number("+20 100 123 4567", "Hello")
        env.fake.page.goto.assert_awaited_once_with(
            "https://web.whatsapp.com/send?phone=201001234567", wait_until="domcontentloaded"
        )
        env.fake.page.keyboard.insert_text.assert_awaited_once_with("Hello")

    async def test_malformed_number_fails_before_navigating(self, env):
        with pytest.raises(InvalidPhoneNumberError):
            await env.wa.send_message_to_number("abc", "hi")
        env.fake.page.goto.assert_not_awaited()

    async def test_blank_message_raises(self, env):
        with pytest.raises(ValueError):
            await env.wa.send_message_to_number("+201001234567", " ")

    async def test_invalid_number_dialog_is_dismissed_and_reported(self, env):
        env.fake.remove("message_composer")
        env.fake.add("modal_dialog")
        button = env.fake.add("modal_button")
        with pytest.raises(InvalidPhoneNumberError, match="invalid"):
            await env.wa.send_message_to_number("+201001234567", "hi")
        button.click.assert_awaited_once()

    async def test_invalid_number_dialog_without_button(self, env):
        env.fake.remove("message_composer")
        env.fake.add("modal_dialog")
        with pytest.raises(InvalidPhoneNumberError):
            await env.wa.send_message_to_number("+201001234567", "hi")

    async def test_qr_screen_means_session_expired(self, env):
        env.fake.remove("message_composer")
        env.fake.add("qr_code")
        with pytest.raises(SessionExpiredError):
            await env.wa.send_message_to_number("+201001234567", "hi")

    async def test_chat_that_never_opens_raises(self, env):
        env.fake.remove("message_composer")
        with pytest.raises(ElementNotFoundError, match="did not open"):
            await env.wa.send_message_to_number("+201001234567", "hi")


class TestGetLastMessages:
    async def test_returns_structured_messages_oldest_first(self, env):
        set_rows(env, [raw_message("false_1", "hey"), raw_message("true_2", "yo")])
        messages = await env.wa.get_last_messages("Alice")
        assert [m.text for m in messages] == ["hey", "yo"]
        assert [m.direction for m in messages] == [MessageDirection.INCOMING, MessageDirection.OUTGOING]
        assert messages[0].timestamp.year == 2026 and messages[0].sender == "Alice"

    async def test_limit_keeps_only_the_newest(self, env):
        set_rows(env, [raw_message(f"false_{i}", str(i)) for i in range(5)])
        messages = await env.wa.get_last_messages("Alice", limit=2)
        assert [m.text for m in messages] == ["3", "4"]

    async def test_empty_chat_returns_empty_list(self, env):
        assert await env.wa.get_last_messages("Alice") == []

    @pytest.mark.parametrize("limit", [0, -3])
    async def test_invalid_limit(self, env, limit):
        with pytest.raises(ValueError):
            await env.wa.get_last_messages("Alice", limit=limit)

    async def test_unknown_contact(self, env):
        with pytest.raises(ContactNotFoundError):
            await env.wa.get_last_messages("Zed")


class TestGetUnreadChats:
    async def test_returns_only_unread_named_chats(self, env):
        rows = env.fake.add("chat_rows", match_count=3)
        raws = [
            {"name": "Alice", "hasBadge": True, "badgeText": "2"},
            {"name": "Bob", "hasBadge": False},
            {"name": "Eve", "hasBadge": True, "badgeText": None, "badgeLabel": "5 unread messages"},
        ]
        locators = []
        for raw in raws:
            row = make_locator()
            row.evaluate = AsyncMock(return_value=raw)
            locators.append(row)
        rows.nth = lambda index: locators[index]
        chats = await env.wa.get_unread_chats()
        assert [(c.name, c.unread_count) for c in chats] == [("Alice", 2), ("Eve", 5)]

    async def test_no_rows(self, env):
        assert await env.wa.get_unread_chats() == []

    async def test_qr_screen_means_session_expired(self, env):
        env.fake.add("qr_code")
        with pytest.raises(SessionExpiredError):
            await env.wa.get_unread_chats()

    async def test_chat_list_missing(self, env):
        env.fake.remove("chat_list")
        with pytest.raises(ElementNotFoundError):
            await env.wa.get_unread_chats()


class TestIsOnline:
    async def test_online(self, env):
        env.fake.add("online_indicator")
        assert await env.wa.is_online("Alice") is True

    async def test_offline(self, env):
        assert await env.wa.is_online("Alice") is False


class TestWaitForMessage:
    async def test_returns_first_new_incoming_message(self, env, monkeypatch):
        reads = AsyncMock(side_effect=[
            [incoming("false_1")],
            [incoming("false_1")],
            [incoming("false_1"), incoming("false_2", "new")],
        ])
        monkeypatch.setattr(env.wa, "_read_recent_messages", reads)
        message = await env.wa.wait_for_message("Alice", timeout_s=5)
        assert message.message_id == "false_2" and message.text == "new"

    async def test_ignores_new_outgoing_messages(self, env, monkeypatch):
        outgoing = ChatMessage("mine", MessageDirection.OUTGOING, message_id="true_9")
        monkeypatch.setattr(env.wa, "_read_recent_messages", AsyncMock(return_value=[outgoing]))
        # baseline read already contains it, so nothing is new: times out
        with pytest.raises(MessageTimeoutError, match="Alice"):
            await env.wa.wait_for_message("Alice", timeout_s=0.02)

    async def test_new_outgoing_message_is_skipped_incoming_returned(self, env, monkeypatch):
        mine = ChatMessage("mine", MessageDirection.OUTGOING, message_id="true_9")
        reads = AsyncMock(side_effect=[[], [mine, incoming("false_3", "theirs")]])
        monkeypatch.setattr(env.wa, "_read_recent_messages", reads)
        assert (await env.wa.wait_for_message("Alice", timeout_s=5)).text == "theirs"

    async def test_times_out_when_nothing_arrives(self, env):
        set_rows(env, [raw_message("false_1")])
        with pytest.raises(MessageTimeoutError):
            await env.wa.wait_for_message("Alice", timeout_s=0.02)


@asynccontextmanager
async def _file_chooser_context(chooser):
    """Stand-in for ``page.expect_file_chooser()`` that yields ``chooser``."""
    info = MagicMock()

    async def resolve():
        return chooser

    info.value = resolve()
    yield info


class TestSendFile:
    @pytest.fixture
    def file_inputs(self, env):
        env.fake.add("attach_button")
        env.fake.add("media_send_button")
        return env.fake.add("media_file_input"), env.fake.add("document_file_input")

    async def test_image_uses_media_input_with_caption(self, env, file_inputs, tmp_path):
        image = tmp_path / "pic.png"
        image.write_bytes(b"x")
        caption = env.fake.add("caption_input")
        await env.wa.send_file("Alice", image, caption="look")
        media_input, document_input = file_inputs
        media_input.set_input_files.assert_awaited_once_with(str(image))
        document_input.set_input_files.assert_not_awaited()
        caption.click.assert_awaited_once()
        env.fake.page.keyboard.insert_text.assert_awaited_once_with("look")
        env.fake.registry["media_send_button"].click.assert_awaited_once()

    async def test_video_is_treated_as_media(self, env, file_inputs, tmp_path):
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"x")
        await env.wa.send_file("Alice", str(video))
        file_inputs[0].set_input_files.assert_awaited_once()

    @pytest.mark.parametrize("name", ["doc.pdf", "data.unknownext"])
    async def test_other_files_use_document_input_without_caption(self, env, file_inputs, tmp_path, name):
        document = tmp_path / name
        document.write_bytes(b"x")
        await env.wa.send_file("Alice", document)
        file_inputs[1].set_input_files.assert_awaited_once_with(str(document))
        env.fake.page.keyboard.insert_text.assert_not_awaited()

    async def test_missing_file_raises_before_opening_chat(self, env, tmp_path):
        with pytest.raises(AttachmentError):
            await env.wa.send_file("Alice", tmp_path / "nope.txt")
        env.fake.registry["search_box"].click.assert_not_awaited()

    async def test_directory_is_not_a_file(self, env, tmp_path):
        with pytest.raises(AttachmentError):
            await env.wa.send_file("Alice", tmp_path)

    async def test_missing_attach_button_is_retried_then_raises(self, env, tmp_path):
        document = tmp_path / "doc.pdf"
        document.write_bytes(b"x")
        with pytest.raises(ElementNotFoundError):
            await env.wa.send_file("Alice", document)

    @pytest.mark.parametrize(
        ("name", "menu_item", "other_item"),
        [
            ("doc.pdf", "document_menu_item", "media_menu_item"),
            ("pic.png", "media_menu_item", "document_menu_item"),
        ],
    )
    async def test_attach_menu_entry_answers_file_chooser(
        self, env, file_inputs, tmp_path, name, menu_item, other_item
    ):
        attachment = tmp_path / name
        attachment.write_bytes(b"x")
        chosen = env.fake.add(menu_item)
        skipped = env.fake.add(other_item)
        chooser = MagicMock()
        chooser.set_files = AsyncMock()
        env.fake.page.expect_file_chooser = MagicMock(return_value=_file_chooser_context(chooser))
        await env.wa.send_file("Alice", attachment)
        chosen.click.assert_awaited_once()
        skipped.click.assert_not_awaited()
        chooser.set_files.assert_awaited_once_with(str(attachment))
        for file_input in file_inputs:
            file_input.set_input_files.assert_not_awaited()

    async def test_unconfirmed_upload_raises(self, env, file_inputs, tmp_path):
        document = tmp_path / "doc.pdf"
        document.write_bytes(b"x")
        env.fake.on_send = lambda: None
        with pytest.raises(MessageSendError):
            await env.wa.send_file("Alice", document)


class TestCreateGroup:
    @pytest.fixture
    def group_ui(self, env):
        for name in ("new_chat_button", "new_group_item", "group_participant_search",
                     "group_next_button", "group_name_input", "group_create_button"):
            env.fake.add(name)
        env.fake.add(rendered("participant_result_templates", "Alice"))
        env.fake.add(rendered("participant_result_templates", "Bob"))
        env.fake.add(rendered("chat_header_title_templates", "Team"))
        return env

    async def test_happy_path(self, group_ui):
        await group_ui.wa.create_group("Team", [" Alice ", "Bob"])
        registry = group_ui.fake.registry
        registry[rendered("participant_result_templates", "Alice")].click.assert_awaited_once()
        registry["group_name_input"].fill.assert_awaited_once_with("Team")
        registry["group_create_button"].click.assert_awaited_once()

    @pytest.mark.parametrize(
        "name, participants",
        [("", ["A"]), ("Team", []), ("Team", "Alice"), ("Team", ["A", " "])],
    )
    async def test_invalid_arguments(self, group_ui, name, participants):
        with pytest.raises(ValueError):
            await group_ui.wa.create_group(name, participants)

    async def test_unknown_participant(self, group_ui):
        with pytest.raises(ContactNotFoundError, match="Zed"):
            await group_ui.wa.create_group("Team", ["Alice", "Zed"])

    async def test_group_that_does_not_open(self, group_ui):
        group_ui.fake.remove(rendered("chat_header_title_templates", "Team"))
        with pytest.raises(GroupCreationError):
            await group_ui.wa.create_group("Team", ["Alice"])

    async def test_qr_screen(self, group_ui):
        group_ui.fake.add("qr_code")
        with pytest.raises(SessionExpiredError):
            await group_ui.wa.create_group("Team", ["Alice"])

    async def test_missing_menu_button(self, group_ui):
        group_ui.fake.remove("new_chat_button")
        with pytest.raises(ElementNotFoundError):
            await group_ui.wa.create_group("Team", ["Alice"])


class TestLogout:
    @pytest.fixture
    def logout_ui(self, env):
        env.fake.add("menu_button")
        env.fake.add("logout_item")
        env.fake.add("logout_confirm")
        env.fake.add("qr_code")
        env.wa._was_logged_in = True
        return env

    async def test_confirms_and_waits_for_qr(self, logout_ui):
        await logout_ui.wa.logout()
        logout_ui.fake.registry["logout_confirm"].click.assert_awaited_once()
        assert not logout_ui.wa._was_logged_in

    async def test_works_without_confirmation_dialog(self, logout_ui):
        logout_ui.fake.remove("logout_confirm")
        await logout_ui.wa.logout()

    async def test_raises_when_qr_never_returns(self, logout_ui):
        logout_ui.fake.remove("qr_code")
        with pytest.raises(LogoutError):
            await logout_ui.wa.logout()

    async def test_missing_menu(self, logout_ui):
        logout_ui.fake.remove("menu_button")
        with pytest.raises(ElementNotFoundError):
            await logout_ui.wa.logout()


class TestFindFirstFallbacks:
    async def test_second_candidate_is_used_when_first_is_missing(self, env):
        found = env.fake.add("second")
        locator = await env.wa._find_first(("first", "second"))
        assert locator is found

    async def test_all_candidates_missing(self, env):
        with pytest.raises(ElementNotFoundError, match="first"):
            await env.wa._find_first(("first", "second"))

    async def test_empty_candidates_rejected(self, env):
        with pytest.raises(ValueError):
            await env.wa._try_find_first(())

    async def test_state_is_forwarded(self, env):
        locator = env.fake.add("hidden")
        await env.wa._find_first(("hidden",), state="attached")
        assert locator.wait_for.await_args.kwargs["state"] == "attached"

    async def test_zero_timeout_is_not_replaced_by_default_and_stays_finite(self, env):
        locator = env.fake.add("a")
        await env.wa._find_first(("a",), timeout_ms=0)
        assert locator.wait_for.await_args.kwargs["timeout"] == 1

    async def test_fallbacks_race_in_one_wait(self, env):
        env.fake.add("second")
        combined = env.fake.registry["second"]
        await env.wa._find_first(("first", "second"), timeout_ms=500)
        # a single combined wait: the absent primary selector costs no timeout
        env.fake.absent.wait_for.assert_not_awaited()
        assert combined.wait_for.await_count == 0

    async def test_earliest_listed_match_wins_when_several_match(self, env, caplog):
        first = env.fake.add("first")
        env.fake.add("second")
        with caplog.at_level(logging.DEBUG):
            assert await env.wa._find_first(("first", "second")) is first
        assert "Matched selector: first" in caplog.text

    async def test_racing_match_that_vanished_falls_back_to_combined_locator(self, env):
        env.fake.add("a", match_count=0)  # waits fine but is gone when we look
        env.fake.add("b", match_count=0)
        assert await env.wa._find_first(("a", "b")) is not None

    async def test_locate_all_falls_back_to_second_selector(self, env):
        second = env.fake.add("two", match_count=2)
        assert await env.wa._locate_all(("one", "two")) is second

    async def test_wait_for_state_returns_none_on_timeout(self, env):
        assert await env.wa._wait_for_state({"x": ("nothing",)}, 0.01) is None
