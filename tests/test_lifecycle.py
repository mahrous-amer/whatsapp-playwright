"""start/close/context manager and default wiring."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import whatsapp_web
from tests.conftest import make_config
from whatsapp_web import (
    BrowserLaunchError,
    NotStartedError,
    WhatsAppWeb,
    build_default_controls,
)


def make_playwright_mocks(pages=None):
    page = MagicMock()
    context = MagicMock()
    context.pages = [page] if pages is None else pages
    context.new_page = AsyncMock(return_value=page)
    context.close = AsyncMock()
    playwright = MagicMock()
    playwright.chromium.launch_persistent_context = AsyncMock(return_value=context)
    playwright.stop = AsyncMock()
    factory = MagicMock()
    factory.return_value.start = AsyncMock(return_value=playwright)
    return factory, playwright, context, page


@pytest.fixture
def client(tmp_path):
    return WhatsAppWeb(make_config(tmp_path, headless=True, channel="chrome"))


async def test_start_launches_persistent_context_with_options(client, tmp_path):
    factory, playwright, context, page = make_playwright_mocks()
    with patch.object(whatsapp_web, "async_playwright", factory):
        await client.start()
    playwright.chromium.launch_persistent_context.assert_awaited_once_with(
        str(tmp_path / "profile"), headless=True, channel="chrome"
    )
    page.set_default_timeout.assert_called_once_with(20)


async def test_start_creates_page_when_profile_has_none(client):
    factory, _, context, page = make_playwright_mocks(pages=[])
    with patch.object(whatsapp_web, "async_playwright", factory):
        await client.start()
    context.new_page.assert_awaited_once()
    assert client._page is page


async def test_start_is_idempotent(client):
    factory, *_ = make_playwright_mocks()
    with patch.object(whatsapp_web, "async_playwright", factory):
        await client.start()
        await client.start()
    assert factory.call_count == 1


async def test_launch_failure_stops_playwright_and_raises(client):
    factory, playwright, *_ = make_playwright_mocks()
    playwright.chromium.launch_persistent_context.side_effect = RuntimeError("no chrome")
    with patch.object(whatsapp_web, "async_playwright", factory):
        with pytest.raises(BrowserLaunchError, match="no chrome"):
            await client.start()
    playwright.stop.assert_awaited_once()
    assert client._context is None


async def test_close_releases_everything_and_is_idempotent(client):
    factory, playwright, context, _ = make_playwright_mocks()
    with patch.object(whatsapp_web, "async_playwright", factory):
        await client.start()
    await client.close()
    await client.close()
    context.close.assert_awaited_once()
    playwright.stop.assert_awaited_once()
    with pytest.raises(NotStartedError):
        client._require_page()


async def test_close_on_never_started_client_is_a_noop(client):
    await client.close()


async def test_close_logs_errors_and_still_stops_playwright(client, caplog):
    factory, playwright, context, _ = make_playwright_mocks()
    context.close.side_effect = RuntimeError("boom")
    with patch.object(whatsapp_web, "async_playwright", factory):
        await client.start()
    with caplog.at_level(logging.WARNING):
        await client.close()
    playwright.stop.assert_awaited_once()
    assert "closing browser context" in caplog.text


async def test_async_context_manager_starts_and_closes(client):
    factory, playwright, context, _ = make_playwright_mocks()
    with patch.object(whatsapp_web, "async_playwright", factory):
        async with client as entered:
            assert entered is client
        context.close.assert_awaited_once()
    playwright.stop.assert_awaited_once()


async def test_context_manager_closes_when_body_raises(client):
    factory, _, context, _ = make_playwright_mocks()
    with patch.object(whatsapp_web, "async_playwright", factory):
        with pytest.raises(ValueError):
            async with client:
                raise ValueError("body failed")
    context.close.assert_awaited_once()


@pytest.mark.parametrize(
    "call",
    [
        lambda wa: wa.login(),
        lambda wa: wa.send_message("A", "hi"),
        lambda wa: wa.send_message_to_number("+201001234567", "hi"),
        lambda wa: wa.get_last_messages("A"),
        lambda wa: wa.get_unread_chats(),
        lambda wa: wa.is_online("A"),
        lambda wa: wa.logout(),
    ],
)
async def test_operations_before_start_raise_not_started(client, call):
    with pytest.raises(NotStartedError):
        await call(client)


def test_default_controls_use_session_dir_for_safety_state(tmp_path):
    config = make_config(tmp_path)
    controls = build_default_controls(config)
    controls.safety_guard.opt_out("name:x")
    assert (tmp_path / "profile" / "safety_state.json").exists()


def test_default_controls_honour_explicit_state_file(tmp_path):
    from whatsapp_web import SafetyConfig

    state_file = tmp_path / "custom.json"
    config = make_config(tmp_path, safety=SafetyConfig(state_file=state_file))
    build_default_controls(config).safety_guard.opt_out("name:x")
    assert state_file.exists()


def test_client_builds_default_controls_and_safety_api(tmp_path):
    wa = WhatsAppWeb(make_config(tmp_path))
    assert not wa.is_opted_out("Alice")
    wa.opt_out("Alice")
    assert wa.is_opted_out(" alice ")
    wa.opt_in("Alice")
    assert not wa.is_opted_out("Alice")
    wa._controls.safety_guard.trip_breaker("x")
    wa.reset_safety_breaker()
    wa._controls.safety_guard.check_can_send("name:a", None)
