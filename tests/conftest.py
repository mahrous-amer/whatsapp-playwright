"""Shared fakes: a registry-backed Playwright page and a controllable clock."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from whatsapp_web import (
    Clock,
    RateLimitConfig,
    RateLimiter,
    SafetyConfig,
    SafetyGuard,
    SafetyStore,
    SendControls,
    Selectors,
    TypingConfig,
    TypingDelay,
    WhatsAppWeb,
    WhatsAppWebConfig,
)

# Every selector field becomes a single candidate named after the field, so
# tests can register fake locators by field name.
TEST_SELECTORS = Selectors(**{f.name: (f.name,) for f in fields(Selectors)})
TEST_SELECTORS = replace(
    TEST_SELECTORS,
    chat_result_templates=("chat_result_templates[{name}]",),
    participant_result_templates=("participant_result_templates[{name}]",),
    chat_header_title_templates=("chat_header_title_templates[{name}]",),
)


def rendered(field_name: str, name: str) -> str:
    """Selector string produced for a template field and contact name."""
    return f"{field_name}[{json.dumps(name, ensure_ascii=False)}]"


class OutgoingChat:
    """Outgoing messages of the open chat; grows only when a send happens."""

    def __init__(self) -> None:
        self.ids = ["true_0"]
        self.is_pending = False

    def send(self) -> None:
        self.ids.append(f"true_{len(self.ids)}")


def changing_values(*values: Any):
    """AsyncMock side effect returning each value once, then repeating the last."""
    remaining = list(values)

    async def next_value(*_args: Any, **_kwargs: Any) -> Any:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return next_value


def make_locator(present: bool = True, match_count: int = 1) -> MagicMock:
    """Build a Locator double; absent locators time out and match nothing."""
    locator = MagicMock()
    locator.present = present
    locator.or_ = MagicMock(
        side_effect=lambda other: make_locator(present=present or other.present)
    )
    locator.first = locator
    locator.nth = MagicMock(return_value=locator)
    locator.wait_for = AsyncMock(
        side_effect=None if present else PlaywrightTimeoutError("not found")
    )
    locator.is_visible = AsyncMock(return_value=present)
    locator.count = AsyncMock(return_value=match_count if present else 0)
    for method in ("click", "fill", "press", "set_input_files", "evaluate", "inner_text"):
        setattr(locator, method, AsyncMock())
    locator.get_attribute = AsyncMock(return_value="true_1")
    child = MagicMock()
    child.count = AsyncMock(return_value=0)
    locator.locator = MagicMock(return_value=child)
    return locator


class FakePage:
    """Page double whose ``locator()`` looks selectors up in a registry."""

    def __init__(self) -> None:
        self.absent = make_locator(present=False)
        self.registry: dict[str, MagicMock] = {}
        self.page = MagicMock()
        self.page.locator = MagicMock(side_effect=lambda s: self.registry.get(s, self.absent))
        self.page.goto = AsyncMock()
        self.page.set_default_timeout = MagicMock()
        self.page.keyboard = MagicMock()
        self.events: list[tuple] = []
        for method in ("press", "type", "insert_text"):
            setattr(
                self.page.keyboard,
                method,
                AsyncMock(side_effect=self._record_keyboard(method)),
            )
        self.outgoing = OutgoingChat()
        self.on_send = self._deliver_message

    def _record_keyboard(self, method: str):
        def record(text: str, **_kwargs: Any) -> None:
            self.events.append((method, text))

        return record

    def _deliver_message(self) -> None:
        self.events.append(("send",))
        self.outgoing.send()

    def add(self, selector: str, **kwargs: Any) -> MagicMock:
        """Register a present locator under ``selector``."""
        locator = make_locator(**kwargs)
        if selector in ("send_button", "media_send_button"):
            locator.click = AsyncMock(side_effect=lambda *a, **k: self.on_send())
        if selector == "message_composer":
            locator.press = AsyncMock(
                side_effect=lambda key, **k: self.on_send() if key == "Enter" else None
            )
        self.registry[selector] = locator
        return locator

    def remove(self, selector: str) -> None:
        """Make ``selector`` match nothing again."""
        self.registry.pop(selector, None)

    def add_contact(self, name: str) -> MagicMock:
        """Make the chat search find ``name``."""
        locator = self.add(rendered("chat_result_templates", name))
        locator.click = AsyncMock(side_effect=lambda *a, **k: self.events.append(("open", name)))
        return locator


class FakeClock:
    """Deterministic clock whose sleep advances time instantly."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.time = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.time

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.time += seconds

    def as_clock(self) -> Clock:
        return Clock(now=self.now, sleep=self.sleep)


def make_config(tmp_path: Path, **overrides: Any) -> WhatsAppWebConfig:
    """Fast, permissive config rooted in ``tmp_path`` (never the real home)."""
    options: dict[str, Any] = {
        "user_data_dir": tmp_path / "profile",
        "default_timeout_ms": 20,
        "short_timeout_ms": 10,
        "login_timeout_s": 0.02,
        "send_verify_timeout_s": 0.02,
        "upload_verify_timeout_s": 0.02,
        "poll_interval_s": 0,
        "retry_delay_s": 0,
        "rate_limit": RateLimitConfig(
            min_interval_seconds=0, max_per_minute=100, max_per_hour=1000, max_per_day=1000
        ),
        "typing": TypingConfig(min_delay_ms=0, max_delay_ms=0),
    }
    options.update(overrides)
    return WhatsAppWebConfig(**options)


@dataclass
class Env:
    """A started client wired to fakes."""

    wa: WhatsAppWeb
    fake: FakePage
    clock: FakeClock
    config: WhatsAppWebConfig
    guard: SafetyGuard
    state_path: Path


def build_env(tmp_path: Path, **config_overrides: Any) -> Env:
    """Create a started client with a standard fake chat UI."""
    config = make_config(tmp_path, **config_overrides)
    clock = FakeClock()
    state_path = tmp_path / "safety.json"
    guard = SafetyGuard(config.safety, SafetyStore(state_path), clock.as_clock())
    controls = SendControls(
        rate_limiter=RateLimiter(config.rate_limit, clock.as_clock(), guard.send_history),
        typing_delay=TypingDelay(config.typing, random.Random(1)),
        safety_guard=guard,
    )
    wa = WhatsAppWeb(config, TEST_SELECTORS, controls)
    fake = FakePage()
    for selector in ("chat_list", "search_box", "message_composer", "send_button"):
        fake.add(selector)
    fake.add_contact("Alice")
    outgoing = fake.add("outgoing_messages")
    outgoing.count = AsyncMock(side_effect=lambda: len(fake.outgoing.ids))
    outgoing.get_attribute = AsyncMock(side_effect=lambda name: fake.outgoing.ids[-1])
    outgoing.locator.return_value.count = AsyncMock(
        side_effect=lambda: 1 if fake.outgoing.is_pending else 0
    )
    wa._page = fake.page
    wa._context = MagicMock()
    return Env(wa, fake, clock, config, guard, state_path)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return build_env(tmp_path)


@pytest.fixture
def make_env(tmp_path: Path):
    def factory(**config_overrides: Any) -> Env:
        return build_env(tmp_path, **config_overrides)

    return factory
