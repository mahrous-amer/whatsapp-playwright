"""Async Playwright automation for WhatsApp Web.

Usage::

    config = WhatsAppWebConfig(headless=False, user_data_dir="~/.wa-session")
    async with WhatsAppWeb(config) as whatsapp:
        await whatsapp.login()
        await whatsapp.send_message("Some Contact", "Hello!")

The browser profile lives in ``user_data_dir`` so the QR code only has to be
scanned on the first run.

WhatsApp Web is a private, frequently changing application. Every selector the
class relies on lives in :class:`Selectors`, each with ordered fallbacks, so a
DOM change usually means editing (or overriding) that one dataclass.
Automating WhatsApp may violate its Terms of Service; use it responsibly.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import logging
import mimetypes
import os
import random
import re
import tempfile
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, NoReturn, TypeVar

try:
    import fcntl
except ImportError:  # pragma: no cover - not available on Windows
    fcntl = None  # type: ignore[assignment]

from playwright.async_api import (
    BrowserContext,
    Error as PlaywrightError,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

logger = logging.getLogger(__name__)

WHATSAPP_WEB_URL = "https://web.whatsapp.com"
SEND_TO_NUMBER_URL_TEMPLATE = WHATSAPP_WEB_URL + "/send?phone={phone}"

MIN_PHONE_DIGITS = 8
MAX_PHONE_DIGITS = 15  # E.164 upper bound
MESSAGE_SCAN_WINDOW = 30
SECONDS_PER_DAY = 86_400.0
MAX_PROCESSED_OPT_OUTS = 1000
CLOCK_SKEW_TOLERANCE_S = 300.0
SAFETY_STATE_FILENAME = "safety_state.json"

# WhatsApp's warning wording changes and is localised; this matches English
# text of ban / restriction / spam banners only. Extend it for other languages.
ACCOUNT_RISK_TEXT_PATTERN = re.compile(
    r"temporarily banned|account (?:is )?(?:at risk|restricted|banned)"
    r"|unusual activity|spam|violat\w+ (?:our )?terms",
    re.IGNORECASE,
)

# Order matters: the first format that parses wins. "%m/%d/%Y" is tried before
# "%d/%m/%Y", so day-first locales with day <= 12 are misread. Adjust to your
# account's locale if you rely on timestamps.
TIMESTAMP_TIME_FORMATS = ("%H:%M", "%I:%M %p")
TIMESTAMP_DATE_FORMATS = ("%m/%d/%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%d")

_PRE_PLAIN_TEXT_PATTERN = re.compile(
    r"^\[(?P<time>[^,\]]+),\s*(?P<date>[^\]]+)\]\s*(?P<sender>.*?):\s*$"
)

T = TypeVar("T")


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------
class WhatsAppError(Exception):
    """Base class for every error raised by this module."""


class NotStartedError(WhatsAppError):
    """The browser session was used before ``start()`` (or after ``close()``)."""


class BrowserLaunchError(WhatsAppError):
    """The persistent browser context could not be launched."""


class LoginTimeoutError(WhatsAppError):
    """The chat list did not appear before the login timeout (QR not scanned)."""


class SessionExpiredError(WhatsAppError):
    """WhatsApp is showing the QR code: the saved session is no longer valid."""


class ElementNotFoundError(WhatsAppError):
    """None of the fallback selectors for a UI element matched."""


class ContactNotFoundError(WhatsAppError):
    """The contact or group could not be found in the chat search."""


class AmbiguousContactError(ContactNotFoundError):
    """More than one chat has exactly this name, so none is opened.

    Rename one of the contacts, or use ``send_message_to_number``.
    """


class InvalidPhoneNumberError(WhatsAppError):
    """The phone number is malformed or WhatsApp reported it as invalid."""


class MessageSendError(WhatsAppError):
    """The message was typed but never confirmed as sent."""


class MessageUnconfirmedError(MessageSendError):
    """The message appeared in the chat but was still pending at the timeout.

    It may have been delivered, so do not blindly retry: check the chat first.
    """


class MessageTimeoutError(WhatsAppError):
    """No new incoming message arrived before the timeout."""


class AttachmentError(WhatsAppError):
    """The file to send is missing or is not a regular file."""


class GroupCreationError(WhatsAppError):
    """The new group did not open after completing the creation flow."""


class LogoutError(WhatsAppError):
    """The logout flow finished without returning to the QR screen."""


class RateLimitExceededError(WhatsAppError):
    """A send was refused by the rate limiter configured with ``on_limit="raise"``."""


class SafetyViolationError(WhatsAppError):
    """A send was blocked by an account-health guardrail (see SafetyConfig)."""


class AccountAtRiskError(WhatsAppError):
    """The safety breaker is tripped; sends stay halted until it is reset."""


# --------------------------------------------------------------------------
# Data types
# --------------------------------------------------------------------------
class MessageDirection(str, Enum):
    """Whether a message was received or sent by the logged-in account."""

    INCOMING = "incoming"
    OUTGOING = "outgoing"


@dataclass(frozen=True)
class ChatMessage:
    """A single message read from an open chat."""

    text: str
    direction: MessageDirection
    timestamp: datetime | None = None
    sender: str | None = None
    message_id: str | None = None


@dataclass(frozen=True)
class UnreadChat:
    """A chat in the chat list that has unread messages."""

    name: str
    unread_count: int


@dataclass(frozen=True)
class RateLimitConfig:
    """Limits on how fast messages may be sent.

    ``on_limit`` is ``"wait"`` (sleep until a send is allowed) or ``"raise"``
    (raise RateLimitExceededError immediately).
    """

    min_interval_seconds: float = 3.0
    max_per_minute: int = 10
    max_per_hour: int = 60
    max_per_day: int = 200
    on_limit: Literal["wait", "raise"] = "wait"

    def __post_init__(self) -> None:
        """Reject values that would make sending impossible or ambiguous."""
        if self.min_interval_seconds < 0:
            raise ValueError("min_interval_seconds cannot be negative")
        if min(self.max_per_minute, self.max_per_hour, self.max_per_day) < 1:
            raise ValueError("max_per_minute, max_per_hour and max_per_day must be at least 1")
        if self.on_limit not in ("wait", "raise"):
            raise ValueError("on_limit must be 'wait' or 'raise'")


@dataclass(frozen=True)
class TypingConfig:
    """Per-character typing delay, in milliseconds, chosen uniformly in a range.

    Set both bounds to 0 to disable. ``max_total_seconds`` caps the time
    spent typing one text so long messages do not take minutes.
    """

    min_delay_ms: float = 30.0
    max_delay_ms: float = 80.0
    max_total_seconds: float = 10.0

    def __post_init__(self) -> None:
        """Reject inverted ranges and non-positive caps."""
        if self.min_delay_ms < 0 or self.max_delay_ms < self.min_delay_ms:
            raise ValueError("Need 0 <= min_delay_ms <= max_delay_ms")
        if self.max_total_seconds <= 0:
            raise ValueError("max_total_seconds must be positive")


def _normalize_text(text: str) -> str:
    """Collapse whitespace and case so equivalent texts compare equal."""
    return " ".join(text.split()).casefold()


def _normalize_for_matching(text: str) -> str:
    """Like ``_normalize_text`` but also drops punctuation and emoji."""
    return _normalize_text(re.sub(r"[^\w\s]|_", " ", text))


@dataclass(frozen=True)
class SafetyConfig:
    """Account-health guardrails. Defaults are deliberately conservative.

    ``state_file`` defaults to ``<user_data_dir>/safety_state.json``.
    """

    max_new_recipients_per_day: int = 10
    max_identical_recipients: int = 5
    max_groups_per_day: int = 2
    opt_out_keywords: frozenset[str] = frozenset({"stop", "unsubscribe"})
    max_consecutive_failures: int = 3
    state_file: str | Path | None = None

    def __post_init__(self) -> None:
        """Validate limits and normalise keywords and the state path."""
        if min(
            self.max_new_recipients_per_day,
            self.max_identical_recipients,
            self.max_groups_per_day,
            self.max_consecutive_failures,
        ) < 1:
            raise ValueError("safety limits must be at least 1")
        normalized_keywords = frozenset(
            keyword
            for keyword in (_normalize_for_matching(word) for word in self.opt_out_keywords)
            if keyword
        )
        object.__setattr__(self, "opt_out_keywords", normalized_keywords)
        if self.state_file is not None:
            object.__setattr__(self, "state_file", Path(self.state_file).expanduser())


@dataclass(frozen=True)
class Clock:
    """Wall-clock time and sleep, injectable for deterministic tests.

    Wall-clock time (not monotonic) is used because limits persist across
    restarts through the safety state file.
    """

    now: Callable[[], float] = time.time
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


@dataclass
class WhatsAppWebConfig:
    """Tunable behaviour of :class:`WhatsAppWeb`."""

    headless: bool = False
    user_data_dir: str | Path = Path.home() / ".whatsapp-playwright" / "session"
    default_timeout_ms: int = 30_000
    short_timeout_ms: int = 3_000
    login_timeout_s: float = 120.0
    send_verify_timeout_s: float = 15.0
    upload_verify_timeout_s: float = 120.0
    poll_interval_s: float = 0.5
    retry_attempts: int = 3
    retry_delay_s: float = 1.0
    channel: str | None = None  # e.g. "chrome" to use the installed browser
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    typing: TypingConfig = field(default_factory=TypingConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)

    def __post_init__(self) -> None:
        """Normalise the profile path and reject nonsensical values."""
        self.user_data_dir = Path(self.user_data_dir).expanduser()
        if self.retry_attempts < 1:
            raise ValueError("retry_attempts must be at least 1")
        if self.default_timeout_ms <= 0 or self.short_timeout_ms <= 0:
            raise ValueError("timeouts must be positive")
        if self.poll_interval_s < 0 or self.retry_delay_s < 0:
            raise ValueError("poll_interval_s and retry_delay_s cannot be negative")


@dataclass(frozen=True)
class RetryPolicy:
    """How :func:`retry_async` retries a failing operation."""

    attempts: int
    delay_seconds: float
    retry_on: tuple[type[BaseException], ...] = (Exception,)


@dataclass(frozen=True)
class _OutgoingSnapshot:
    """State of the newest outgoing message, used to confirm a send."""

    count: int
    last_message_id: str | None
    is_pending: bool

    def is_same_message_as(self, other: _OutgoingSnapshot) -> bool:
        """Return True when both snapshots point at the same last message.

        Ids are authoritative when both are known: the count can change
        without a new message (WhatsApp virtualises long chats).
        """
        if self.last_message_id is not None and other.last_message_id is not None:
            return self.last_message_id == other.last_message_id
        return (self.count, self.last_message_id) == (other.count, other.last_message_id)


class InMemorySendHistory:
    """Send timestamps kept in memory only (the RateLimiter default)."""

    def __init__(self) -> None:
        """Start with no recorded sends."""
        self._sent_at: list[float] = []

    def times(self) -> list[float]:
        """Return recorded send times, oldest first."""
        return sorted(self._sent_at)

    def add(self, sent_at: float) -> None:
        """Record one send."""
        self._sent_at.append(sent_at)


class RateLimiter:
    """Sliding-window limiter for sends (interval, minute, hour and day).

    The minute and hour windows wait or raise per ``on_limit``. The daily cap
    always raises SafetyViolationError: sleeping for hours is never helpful.
    """

    _MINUTE_SECONDS = 60.0
    _HOUR_SECONDS = 3600.0

    def __init__(
        self,
        config: RateLimitConfig,
        clock: Clock | None = None,
        history: InMemorySendHistory | PersistentSendHistory | None = None,
    ) -> None:
        """Create a limiter; pass a persistent ``history`` to survive restarts."""
        self._config = config
        self._clock = clock or Clock()
        self._history = InMemorySendHistory() if history is None else history

    async def acquire(self) -> None:
        """Reserve a send slot, waiting or raising according to ``on_limit``."""
        while True:
            sent_times = self._history.times()
            now = self._monotonic_wall_time(sent_times)
            recent_sends = [t for t in sent_times if now - t < SECONDS_PER_DAY]
            self._enforce_daily_cap(len(recent_sends))
            wait_seconds = self._seconds_until_allowed(now, recent_sends)
            if wait_seconds <= 0:
                self._history.add(now)
                return
            if self._config.on_limit == "raise":
                raise RateLimitExceededError(
                    f"Send rate limit reached; retry in {wait_seconds:.1f}s"
                )
            logger.info("Rate limit: waiting %.1fs before sending", wait_seconds)
            await self._clock.sleep(wait_seconds)

    def _monotonic_wall_time(self, sent_times: Sequence[float]) -> float:
        """Current time, never earlier than the newest recorded send.

        Protects the windows when the wall clock jumps backwards.
        """
        return max(self._clock.now(), max(sent_times, default=0.0))

    def _enforce_daily_cap(self, sends_today: int) -> None:
        """Raise SafetyViolationError once the daily cap is used up."""
        if sends_today >= self._config.max_per_day:
            raise SafetyViolationError(
                f"Daily send cap of {self._config.max_per_day} reached; try again tomorrow"
            )

    def _seconds_until_allowed(self, now: float, recent_sends: Sequence[float]) -> float:
        """Return how long until all short windows permit a send (<= 0 means now)."""
        return max(
            self._interval_wait(now, recent_sends),
            self._window_wait(now, recent_sends, self._MINUTE_SECONDS, self._config.max_per_minute),
            self._window_wait(now, recent_sends, self._HOUR_SECONDS, self._config.max_per_hour),
        )

    def _interval_wait(self, now: float, recent_sends: Sequence[float]) -> float:
        """Time left until ``min_interval_seconds`` has passed since the last send."""
        if not recent_sends:
            return 0.0
        return recent_sends[-1] + self._config.min_interval_seconds - now

    @staticmethod
    def _window_wait(
        now: float, recent_sends: Sequence[float], window_seconds: float, max_sends: int
    ) -> float:
        """Time left until the window holds fewer than ``max_sends`` sends."""
        in_window = [t for t in recent_sends if now - t < window_seconds]
        if len(in_window) < max_sends:
            return 0.0
        oldest_blocking = in_window[len(in_window) - max_sends]
        return oldest_blocking + window_seconds - now


class TypingDelay:
    """Chooses the per-character delay for ``keyboard.type``."""

    def __init__(self, config: TypingConfig, rng: random.Random | None = None) -> None:
        """Use ``rng`` (injectable for tests) to add jitter within the range."""
        self._config = config
        self._rng = rng or random.Random()

    def delay_ms_for(self, text: str) -> float:
        """Return the delay per character, or 0 when typing delay is disabled."""
        if self._config.max_delay_ms <= 0:
            return 0.0
        jittered_delay = self._rng.uniform(self._config.min_delay_ms, self._config.max_delay_ms)
        budget_per_char = self._config.max_total_seconds * 1000 / max(len(text), 1)
        return min(jittered_delay, budget_per_char)


@dataclass
class SafetyState:
    """Everything the safety guard remembers; persisted as JSON."""

    send_times: deque[float] = field(default_factory=deque)
    new_recipients: list[tuple[float, str]] = field(default_factory=list)
    broadcasts: list[tuple[float, str, str]] = field(default_factory=list)
    group_creations: list[float] = field(default_factory=list)
    opted_out: set[str] = field(default_factory=set)
    known_senders: set[str] = field(default_factory=set)
    processed_opt_outs: list[str] = field(default_factory=list)
    identity_links: dict[str, str] = field(default_factory=dict)
    consecutive_failures: int = 0
    breaker_reason: str | None = None

    def canonical(self, key: str) -> str:
        """Resolve a recipient key to the identity it was linked to."""
        return self.identity_links.get(key, key)

    def link_identities(self, first: str, second: str) -> None:
        """Treat two keys (e.g. a phone and a saved name) as one recipient."""
        keep, merged = self.canonical(first), self.canonical(second)
        if keep == merged:
            return

        def rename(key: str) -> str:
            return keep if key == merged else key

        self.identity_links = {k: rename(v) for k, v in self.identity_links.items()}
        self.identity_links[merged] = keep
        self.opted_out = {rename(key) for key in self.opted_out}
        self.known_senders = {rename(key) for key in self.known_senders}
        self.new_recipients = [(t, rename(key)) for t, key in self.new_recipients]
        self.broadcasts = [(t, digest, rename(key)) for t, digest, key in self.broadcasts]

    def forget_entries_older_than(self, cutoff: float) -> None:
        """Drop daily counters recorded before ``cutoff``."""
        self.new_recipients = [e for e in self.new_recipients if e[0] > cutoff]
        self.broadcasts = [e for e in self.broadcasts if e[0] > cutoff]
        self.group_creations = [t for t in self.group_creations if t > cutoff]

    def has_entries_after(self, cutoff: float) -> bool:
        """Return True if any timestamp lies in the future beyond ``cutoff``."""
        return any(
            t > cutoff
            for t in [
                *self.send_times,
                *(e[0] for e in self.new_recipients),
                *(e[0] for e in self.broadcasts),
                *self.group_creations,
            ]
        )

    def drop_entries_after(self, cutoff: float) -> None:
        """Remove timestamps in the future (clock was wrong when written)."""
        self.send_times = deque(t for t in self.send_times if t <= cutoff)
        self.new_recipients = [e for e in self.new_recipients if e[0] <= cutoff]
        self.broadcasts = [e for e in self.broadcasts if e[0] <= cutoff]
        self.group_creations = [t for t in self.group_creations if t <= cutoff]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable snapshot."""
        return {
            "send_times": list(self.send_times),
            "new_recipients": [list(entry) for entry in self.new_recipients],
            "broadcasts": [list(entry) for entry in self.broadcasts],
            "group_creations": list(self.group_creations),
            "opted_out": sorted(self.opted_out),
            "known_senders": sorted(self.known_senders),
            "processed_opt_outs": list(self.processed_opt_outs),
            "identity_links": dict(self.identity_links),
            "consecutive_failures": self.consecutive_failures,
            "breaker_reason": self.breaker_reason,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> SafetyState:
        """Rebuild state from ``to_dict`` output; malformed data raises."""
        breaker_reason = raw.get("breaker_reason")
        processed = [str(value) for value in raw.get("processed_opt_outs", [])]
        return cls(
            send_times=deque(sorted(float(value) for value in raw.get("send_times", []))),
            new_recipients=[(float(t), str(k)) for t, k in raw.get("new_recipients", [])],
            broadcasts=[(float(t), str(d), str(k)) for t, d, k in raw.get("broadcasts", [])],
            group_creations=sorted(float(value) for value in raw.get("group_creations", [])),
            opted_out={str(value) for value in raw.get("opted_out", [])},
            known_senders={str(value) for value in raw.get("known_senders", [])},
            processed_opt_outs=processed[-MAX_PROCESSED_OPT_OUTS:],
            identity_links={str(k): str(v) for k, v in dict(raw.get("identity_links", {})).items()},
            consecutive_failures=int(raw.get("consecutive_failures", 0)),
            breaker_reason=None if breaker_reason is None else str(breaker_reason),
        )


class SafetyStore:
    """JSON persistence for SafetyState, safe for several processes.

    Every change is applied as reload -> mutate -> atomic write while holding
    an advisory file lock (POSIX), so concurrent instances do not lose each
    other's updates. Failing closed: an unreadable file or a failed write
    trips the circuit breaker instead of silently resetting limits.
    """

    UNREADABLE_REASON = "unreadable safety state"

    def __init__(self, path: Path) -> None:
        """Load existing state from ``path``."""
        self._path = path
        self.state = SafetyState()
        self.refresh()

    def refresh(self) -> None:
        """Pick up changes written by other processes."""
        if not self._path.exists():
            return
        try:
            with self._exclusive_lock():
                self._reload_from_disk()
        except OSError as error:
            self._trip_for_persistence_failure(error)

    def update(self, mutation: Callable[[SafetyState], None]) -> None:
        """Apply ``mutation`` to the freshest state and persist the result."""
        is_applied = False
        try:
            with self._exclusive_lock():
                self._reload_from_disk()
                mutation(self.state)
                is_applied = True
                self._write_atomically()
        except OSError as error:
            if not is_applied:
                mutation(self.state)
            self._trip_for_persistence_failure(error)

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        """Hold an advisory lock on a sidecar file (no-op without fcntl)."""
        if fcntl is None:
            yield
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_name(self._path.name + ".lock")
        with open(lock_path, "a", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def _reload_from_disk(self) -> None:
        """Replace in-memory state with the file's; quarantine it if corrupt."""
        if not self._path.exists():
            return
        try:
            self.state = SafetyState.from_dict(json.loads(self._path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, AttributeError):
            self._quarantine_unreadable_file()

    def _quarantine_unreadable_file(self) -> None:
        """Move the bad file aside and start with the breaker tripped."""
        quarantine_path = self._path.with_name(f"{self._path.name}.corrupt-{int(time.time())}")
        logger.warning(
            "Safety state %s is unreadable; moved to %s and halting sends",
            self._path,
            quarantine_path,
        )
        with contextlib.suppress(OSError):
            self._path.rename(quarantine_path)
        self.state = SafetyState(breaker_reason=self.UNREADABLE_REASON)
        self._write_atomically()  # persist the tripped breaker so a restart cannot forget it

    def _write_atomically(self) -> None:
        """Write to a unique temp file, fsync it, then rename over the target."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", dir=self._path.parent, prefix=".safety-", suffix=".tmp",
            delete=False, encoding="utf-8",
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            try:
                temporary_file.write(json.dumps(self.state.to_dict()))
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
                os.replace(temporary_path, self._path)
            except BaseException:
                temporary_path.unlink(missing_ok=True)
                raise

    def _trip_for_persistence_failure(self, error: OSError) -> None:
        """Halt sending in memory when limits can no longer be persisted."""
        logger.warning("Could not access safety state %s: %s", self._path, error)
        self.state.breaker_reason = f"could not persist safety state ({error})"


class PersistentSendHistory:
    """Send history stored in the SafetyStore, shared across processes."""

    def __init__(self, store: SafetyStore) -> None:
        """Bind the history to a store."""
        self._store = store

    def times(self) -> list[float]:
        """Return the latest persisted send times, oldest first."""
        self._store.refresh()
        return sorted(self._store.state.send_times)

    def add(self, sent_at: float) -> None:
        """Persist one send, dropping entries older than a day."""

        def mutation(state: SafetyState) -> None:
            recent = [t for t in state.send_times if sent_at - t < SECONDS_PER_DAY]
            state.send_times = deque(sorted([*recent, sent_at]))

        self._store.update(mutation)


class SafetyGuard:
    """Enforces account-health guardrails and remembers them across runs.

    Guardrails: new-recipient cap, duplicate-broadcast cap, group-creation
    cap, opt-outs, and a circuit breaker that halts all sending once
    tripped. Recipients are identified by keys from ``recipient_key``.
    """

    def __init__(self, config: SafetyConfig, store: SafetyStore, clock: Clock) -> None:
        """Bind the guard to its configuration, persistence and clock."""
        self._config = config
        self._store = store
        self._clock = clock
        self._drop_future_dated_entries()

    @property
    def send_history(self) -> PersistentSendHistory:
        """Persistent send history for the RateLimiter."""
        return PersistentSendHistory(self._store)

    @property
    def send_times(self) -> list[float]:
        """Currently known send times (for inspection)."""
        return sorted(self._store.state.send_times)

    def _drop_future_dated_entries(self) -> None:
        """Discard timestamps far in the future, e.g. written under a wrong clock."""
        cutoff = self._clock.now() + CLOCK_SKEW_TOLERANCE_S
        if not self._store.state.has_entries_after(cutoff):
            return
        logger.warning("Dropping safety state entries dated in the future")
        self._store.update(lambda state: state.drop_entries_after(cutoff))

    # ---- Checks and bookkeeping for sends ---------------------------------
    def precheck_send(
        self, recipients: Sequence[str], content: str | None, creates_group: bool = False
    ) -> None:
        """Run the checks that need no chat history (everything but the new-recipient cap)."""
        self.ensure_breaker_not_tripped()
        keys = self._canonical_keys(recipients)
        for key in keys:
            if key in self._store.state.opted_out:
                raise SafetyViolationError(f"{key} opted out; refusing to send")
        self._check_identical_message_cap(keys, content)
        if creates_group:
            self._check_group_cap()

    def check_can_send(
        self, recipients: Sequence[str], content: str | None, creates_group: bool = False
    ) -> None:
        """Raise unless contacting ``recipients`` with ``content`` is allowed.

        Call after the chat history was reviewed, so "has written to us" is known.
        """
        self.precheck_send(recipients, content, creates_group)
        self._check_new_recipient_cap(self._canonical_keys(recipients))

    def _canonical_keys(self, recipients: Sequence[str]) -> list[str]:
        """Resolve linked identities."""
        return [self._store.state.canonical(recipient) for recipient in recipients]

    def record_send(
        self, recipients: Sequence[str], content: str | None, creates_group: bool = False
    ) -> None:
        """Remember a send for the daily counters."""
        now = self._clock.now()
        digest = None if content is None else self._digest(content)

        def mutation(state: SafetyState) -> None:
            state.forget_entries_older_than(now - SECONDS_PER_DAY)
            already_counted = {key for _, key in state.new_recipients}
            for recipient in recipients:
                key = state.canonical(recipient)
                if key not in state.known_senders and key not in already_counted:
                    state.new_recipients.append((now, key))
                    already_counted.add(key)
                if digest is not None:
                    state.broadcasts.append((now, digest, key))
            if creates_group:
                state.group_creations.append(now)

        self._store.update(mutation)

    def _check_new_recipient_cap(self, keys: Sequence[str]) -> None:
        """Block contacting too many people who never wrote to us first."""
        state = self._store.state
        unknown = {key for key in keys if key not in state.known_senders}
        recent = self._recent_new_recipients()
        if len(recent | unknown) <= self._config.max_new_recipients_per_day:
            return
        raise SafetyViolationError(
            f"New-recipient cap of {self._config.max_new_recipients_per_day} per day reached"
        )

    def _check_identical_message_cap(self, keys: Sequence[str], content: str | None) -> None:
        """Block sending the same content to too many distinct recipients."""
        if content is None:
            return
        now = self._clock.now()
        digest = self._digest(content)
        recipients = {
            key
            for sent_at, entry_digest, key in self._store.state.broadcasts
            if entry_digest == digest and now - sent_at < SECONDS_PER_DAY
        }
        if len(recipients | set(keys)) <= self._config.max_identical_recipients:
            return
        raise SafetyViolationError(
            f"Identical message already sent to {len(recipients)} recipients today; "
            f"limit is {self._config.max_identical_recipients}"
        )

    def _check_group_cap(self) -> None:
        """Block creating too many groups per day."""
        now = self._clock.now()
        groups_today = [t for t in self._store.state.group_creations if now - t < SECONDS_PER_DAY]
        if len(groups_today) >= self._config.max_groups_per_day:
            raise SafetyViolationError(
                f"Group-creation cap of {self._config.max_groups_per_day} per day reached"
            )

    def _recent_new_recipients(self) -> set[str]:
        """Unknown recipients contacted within the last day."""
        now = self._clock.now()
        return {
            key
            for sent_at, key in self._store.state.new_recipients
            if now - sent_at < SECONDS_PER_DAY
        }

    @staticmethod
    def _digest(content: str) -> str:
        """Hash normalised content so message text is not stored on disk."""
        return hashlib.sha256(_normalize_text(content).encode("utf-8")).hexdigest()[:16]

    # ---- Incoming messages and opt-outs -----------------------------------
    def has_messaged_us(self, recipient: str) -> bool:
        """Return True if this recipient has sent us a message before."""
        state = self._store.state
        return state.canonical(recipient) in state.known_senders

    def note_incoming(self, recipient: str, message_id: str, text: str) -> None:
        """Record an incoming message; an opt-out request opts the sender out."""
        state = self._store.state
        is_opt_out = self._is_opt_out_request(text)
        is_new_sender = state.canonical(recipient) not in state.known_senders
        is_new_opt_out = is_opt_out and message_id not in state.processed_opt_outs
        if not (is_new_sender or is_new_opt_out):
            return

        def mutation(current: SafetyState) -> None:
            key = current.canonical(recipient)
            current.known_senders.add(key)
            if is_opt_out and message_id not in current.processed_opt_outs:
                logger.info("%s asked to opt out; no further messages will be sent", key)
                current.processed_opt_outs.append(message_id)
                del current.processed_opt_outs[:-MAX_PROCESSED_OPT_OUTS]
                current.opted_out.add(key)

        self._store.update(mutation)

    def _is_opt_out_request(self, text: str) -> bool:
        """Match a keyword as the whole message or its first words.

        Punctuation and emoji are ignored, so "STOP.", "Stop!" and "stop 🙏"
        all count, while "stopping" and "please stop" do not.
        """
        normalized = _normalize_for_matching(text)
        return any(
            normalized == keyword or normalized.startswith(keyword + " ")
            for keyword in self._config.opt_out_keywords
        )

    def opt_out(self, recipient: str) -> None:
        """Refuse all future sends to ``recipient``."""
        self._store.update(lambda state: state.opted_out.add(state.canonical(recipient)))

    def opt_in(self, recipient: str) -> None:
        """Allow sends to ``recipient`` again."""
        self._store.update(lambda state: state.opted_out.discard(state.canonical(recipient)))

    def is_opted_out(self, recipient: str) -> bool:
        """Return True if sends to ``recipient`` are refused."""
        self._store.refresh()
        state = self._store.state
        return state.canonical(recipient) in state.opted_out

    def link_identities(self, first: str, second: str) -> None:
        """Declare that two keys (phone and saved name) are the same person."""
        self._store.update(lambda state: state.link_identities(first, second))

    # ---- Circuit breaker ---------------------------------------------------
    def ensure_breaker_not_tripped(self) -> None:
        """Raise AccountAtRiskError while the breaker is tripped."""
        self._store.refresh()
        reason = self._store.state.breaker_reason
        if reason is not None:
            raise AccountAtRiskError(
                f"Sending halted: {reason}. Investigate, then call reset_safety_breaker()"
            )

    def trip_breaker(self, reason: str) -> None:
        """Halt all sends until ``reset_breaker`` is called."""
        self._store.update(lambda state: self._trip(state, reason))

    @staticmethod
    def _trip(state: SafetyState, reason: str) -> None:
        """Set the breaker reason on ``state`` and log it."""
        logger.warning("Safety breaker tripped: %s", reason)
        state.breaker_reason = reason

    def reset_breaker(self) -> None:
        """Re-enable sending after the operator has investigated."""
        logger.warning("Safety breaker reset by operator")

        def mutation(state: SafetyState) -> None:
            state.breaker_reason = None
            state.consecutive_failures = 0

        self._store.update(mutation)

    def record_failure(self) -> None:
        """Count a failed send; trip the breaker after too many in a row."""

        def mutation(state: SafetyState) -> None:
            state.consecutive_failures += 1
            if state.consecutive_failures >= self._config.max_consecutive_failures:
                self._trip(state, f"{state.consecutive_failures} consecutive send failures")

        self._store.update(mutation)

    def record_success(self) -> None:
        """Reset the consecutive-failure counter after a good send."""
        if self._store.state.consecutive_failures == 0:
            return
        self._store.update(lambda state: setattr(state, "consecutive_failures", 0))


@dataclass
class SendControls:
    """Collaborators that pace and police sending; inject fakes in tests."""

    rate_limiter: RateLimiter
    typing_delay: TypingDelay
    safety_guard: SafetyGuard


def build_default_controls(config: WhatsAppWebConfig) -> SendControls:
    """Wire the standard limiter, typing delay and guard from ``config``."""
    clock = Clock()
    state_path = config.safety.state_file or Path(config.user_data_dir) / SAFETY_STATE_FILENAME
    guard = SafetyGuard(config.safety, SafetyStore(Path(state_path)), clock)
    return SendControls(
        rate_limiter=RateLimiter(config.rate_limit, clock, guard.send_history),
        typing_delay=TypingDelay(config.typing),
        safety_guard=guard,
    )


def recipient_key(contact: str) -> str:
    """Stable identity for a contact name or phone number.

    Strings that are phone numbers map to ``phone:<digits>`` (unsaved chats
    are titled with the number); anything else maps to ``name:<casefolded>``.
    """
    stripped = contact.strip()
    if re.fullmatch(r"\+?[\d\s\-.()]+", stripped):
        try:
            return f"phone:{normalize_phone_number(stripped)}"
        except InvalidPhoneNumberError:
            pass
    return f"name:{_normalize_text(stripped)}"


# --------------------------------------------------------------------------
# Selectors
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Selectors:
    """Every selector used by :class:`WhatsAppWeb`, in one overridable place.

    Each field is a tuple of fallbacks tried in order. Fields ending in
    ``_templates`` contain a ``{name}`` placeholder that is replaced by a
    safely quoted contact name. Override with ``dataclasses.replace``::

        custom = replace(Selectors(), send_button=('button[aria-label="Send"]',))

    WhatsApp ships DOM changes without notice. Most likely to break:
    ``data-testid`` values (internal, renamed often), icon names such as
    ``data-icon="send"``, and anything English-only (``aria-label`` text,
    "online", "unread"), which also differs per UI language.
    """

    # --- Session state -----------------------------------------------------
    chat_list: tuple[str, ...] = (
        '[data-testid="chat-list"]',
        'div[aria-label="Chat list"]',
        "#pane-side",
    )
    # The QR is a canvas with no stable id; data-ref is the most durable hook.
    qr_code: tuple[str, ...] = (
        '[data-testid="qrcode"]',
        "div[data-ref]",
        'canvas[aria-label*="QR" i]',
    )

    # --- Chat search and opening ------------------------------------------
    # Late 2026 builds use a plain <input role="textbox">; older ones a contenteditable div.
    # Don't match on aria-label here: WhatsApp blanks it once the input holds text,
    # so clearing the search afterwards would no longer find the box.
    search_box: tuple[str, ...] = (
        '#side input[role="textbox"]',
        '[data-testid="chat-list-search"]',
        'div[contenteditable="true"][aria-label*="Search" i]',
        '#side div[contenteditable="true"]',
    )
    # Late 2026 builds render chat rows as role="row" grid cells, older ones as listitems.
    chat_result_templates: tuple[str, ...] = (
        '#pane-side [role="row"]:has(span[title={name}])',
        '#pane-side [role="listitem"]:has(span[title={name}])',
        '#pane-side [data-testid="cell-frame-container"]:has(span[title={name}])',
        "#pane-side span[title={name}]",
    )

    # --- Composer and sending ---------------------------------------------
    message_composer: tuple[str, ...] = (
        '[data-testid="conversation-compose-box-input"]',
        'footer div[contenteditable="true"][role="textbox"]',
        'footer div[contenteditable="true"]',
    )
    send_button: tuple[str, ...] = (
        '[data-testid="send"]',
        'footer button[aria-label="Send"]',
        'footer span[data-icon="send"]',
    )

    # --- Messages -----------------------------------------------------------
    # Late 2026 builds dropped the "true_"/"false_" data-id prefix and the
    # message-in/out classes. Own messages carry a delivery-status label
    # (" Sent ", " Delivered ", " Read ", " Pending ") and usually a "You:"
    # label, which is missing on some grouped messages. English-only; the older
    # markers are kept as fallbacks.
    message_rows: tuple[str, ...] = (
        'div[role="row"] div[data-id]',
        'div[data-id^="true_"], div[data-id^="false_"]',
        "div.message-in, div.message-out",
    )
    outgoing_marker: tuple[str, ...] = (
        '[aria-label="You:"]',
        '[aria-label=" Sent "]',
        '[aria-label=" Delivered "]',
        '[aria-label=" Read "]',
        '[aria-label=" Pending "]',
    )
    outgoing_messages: tuple[str, ...] = (
        'div[role="row"] div[data-id]:has([aria-label="You:"], [aria-label=" Sent "], '
        '[aria-label=" Delivered "], [aria-label=" Read "], [aria-label=" Pending "])',
        'div[data-id^="true_"]',
        "div.message-out",
    )
    message_text: tuple[str, ...] = (
        "span.selectable-text.copyable-text",
        '[data-testid="selectable-text"]',
        "span.selectable-text",
    )
    pending_indicator: tuple[str, ...] = (
        '[data-icon="msg-time"]',
        '[aria-label*="Pending" i]',
    )

    # --- Chat list ----------------------------------------------------------
    chat_rows: tuple[str, ...] = (
        '#pane-side [data-testid="cell-frame-container"]',
        '#pane-side [role="listitem"]',
        '#pane-side [role="row"]',
    )
    chat_title: tuple[str, ...] = ("span[title]", '[data-testid="cell-frame-title"]')
    unread_badge: tuple[str, ...] = (
        '[data-testid="icon-unread-count"]',
        'span[aria-label*="unread" i]',
    )

    # --- Presence -----------------------------------------------------------
    # Status text is localised; these only work for English UI.
    online_indicator: tuple[str, ...] = (
        'header span[title="online"]',
        'header span[title^="typing"]',
    )

    # --- Dialogs ------------------------------------------------------------
    modal_dialog: tuple[str, ...] = (
        '[data-animate-modal-popup="true"]',
        'div[role="dialog"]',
    )
    modal_button: tuple[str, ...] = (
        '[data-animate-modal-popup="true"] button',
        'div[role="dialog"] button',
    )

    # --- Attachments --------------------------------------------------------
    attach_button: tuple[str, ...] = (
        '[data-testid="clip"]',
        'button[aria-label="Attach"]',
        'span[data-icon="plus"]',
        'span[data-icon="clip"]',
    )
    # Late 2026 builds: "Attach" opens a menu whose entries create the file input on
    # demand and open a native file chooser. Labels are English-only.
    document_menu_item: tuple[str, ...] = (
        '[role="menuitem"][aria-label="Document"]',
        '[role="menuitem"]:has(span[data-icon*="document"])',
    )
    media_menu_item: tuple[str, ...] = (
        '[role="menuitem"][aria-label="Photos & videos"]',
        '[role="menuitem"][aria-label*="Photos" i]',
    )
    # Older builds keep hidden inputs in the DOM; used when no menu entry appears.
    media_file_input: tuple[str, ...] = (
        'input[type="file"][accept*="image"]',
    )
    document_file_input: tuple[str, ...] = (
        'input[type="file"][accept="*"]',
        'input[type="file"]:not([accept*="image"])',
    )
    caption_input: tuple[str, ...] = (
        '[data-testid^="media-caption-input"][contenteditable="true"]',
        '[data-testid="media-caption-input"]',
        'div[contenteditable="true"][aria-label*="caption" i]',
    )
    # The preview's send button is labelled "Send 1 selected" in late 2026 builds.
    media_send_button: tuple[str, ...] = (
        '[data-testid="media-send"]',
        'div[role="button"][aria-label^="Send "]',
        'div[role="button"][aria-label="Send"]',
        'span[data-icon="send"]',
    )

    # --- Groups -------------------------------------------------------------
    new_chat_button: tuple[str, ...] = (
        '[data-testid="menu-bar-new-chat"]',
        'button[aria-label="New chat"]',
        'span[data-icon="new-chat-outline"]',
    )
    new_group_item: tuple[str, ...] = (
        '[aria-label="New group"]',
        'div[role="button"]:has-text("New group")',
    )
    group_participant_search: tuple[str, ...] = (
        'div[contenteditable="true"][aria-label*="Search" i]',
        'input[type="text"]',
    )
    participant_result_templates: tuple[str, ...] = (
        '[role="listitem"]:has(span[title={name}])',
        "span[title={name}]",
    )
    group_next_button: tuple[str, ...] = (
        '[aria-label="Next"]',
        'span[data-icon="arrow-forward"]',
    )
    group_name_input: tuple[str, ...] = (
        'div[contenteditable="true"][aria-label*="group" i]',
        'div[contenteditable="true"][role="textbox"]',
    )
    group_create_button: tuple[str, ...] = (
        '[aria-label="Create group"]',
        'span[data-icon="checkmark-medium"]',
    )
    # Banners warning about bans or spam. Text is matched by
    # ACCOUNT_RISK_TEXT_PATTERN; both the containers and the wording may change.
    risk_banner: tuple[str, ...] = (
        '[data-animate-modal-popup="true"]',
        'div[role="dialog"]',
        '[data-testid="alert-spam"]',
        'div[role="alert"]',
    )
    chat_header_title: tuple[str, ...] = (
        'header [data-testid="conversation-info-header-chat-title"]',
        "header span[title]",
    )
    chat_header_title_templates: tuple[str, ...] = (
        "header span[title={name}]",
    )

    # --- Logout -------------------------------------------------------------
    # Newer builds move "Log out" under Settings; override logout_item then.
    menu_button: tuple[str, ...] = (
        '[data-testid="menu"]',
        'button[aria-label="Menu"]',
        'span[data-icon="menu"]',
    )
    logout_item: tuple[str, ...] = (
        '[aria-label="Log out"]',
        'div[role="button"]:has-text("Log out")',
        'li:has-text("Log out")',
    )
    logout_confirm: tuple[str, ...] = (
        '[data-testid="popup-controls-ok"]',
        'button:has-text("Log out")',
    )


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------
async def retry_async(operation: Callable[[], Awaitable[T]], policy: RetryPolicy) -> T:
    """Run ``operation`` up to ``policy.attempts`` times.

    Only exceptions listed in ``policy.retry_on`` trigger a retry; the error
    from the final attempt is propagated unchanged.
    """
    for attempt in range(1, policy.attempts):
        try:
            return await operation()
        except policy.retry_on as error:
            logger.warning(
                "Attempt %d/%d failed (%s); retrying in %.1fs",
                attempt,
                policy.attempts,
                error,
                policy.delay_seconds,
            )
            await asyncio.sleep(policy.delay_seconds)
    return await operation()


def normalize_phone_number(raw_phone_number: str) -> str:
    """Return digits-only international format (no ``+``) or raise.

    Accepts spaces, dashes, dots and parentheses plus a leading ``+`` or
    ``00``. A country code is mandatory, so numbers starting with a single
    ``0`` (national format) are rejected rather than guessed.
    """
    cleaned = re.sub(r"[\s\-.()]", "", raw_phone_number)
    if cleaned.startswith("+"):
        cleaned = cleaned[1:]
    elif cleaned.startswith("00"):
        cleaned = cleaned[2:]
    if cleaned.startswith("0"):
        raise InvalidPhoneNumberError(
            f"{raw_phone_number!r} starts with 0; include the country code instead"
        )
    if not re.fullmatch(rf"[0-9]{{{MIN_PHONE_DIGITS},{MAX_PHONE_DIGITS}}}", cleaned):
        raise InvalidPhoneNumberError(
            f"{raw_phone_number!r} must contain {MIN_PHONE_DIGITS}-{MAX_PHONE_DIGITS} "
            "digits including the country code"
        )
    return cleaned


def quote_for_selector(value: str) -> str:
    """Return ``value`` as a double-quoted string literal safe inside a selector."""
    return json.dumps(value, ensure_ascii=False)


def render_selector_templates(templates: Sequence[str], name: str) -> tuple[str, ...]:
    """Fill the ``{name}`` placeholder of each template with a quoted ``name``."""
    quoted_name = quote_for_selector(name)
    return tuple(template.replace("{name}", quoted_name) for template in templates)


def parse_pre_plain_text(pre_plain_text: str | None) -> tuple[datetime | None, str | None]:
    """Parse WhatsApp's ``[10:30, 11/3/2026] Sender: `` attribute.

    Returns ``(timestamp, sender)``; either part is ``None`` when unavailable.
    """
    if not pre_plain_text:
        return None, None
    match = _PRE_PLAIN_TEXT_PATTERN.match(pre_plain_text.strip())
    if match is None:
        return None, None
    time_text = " ".join(match["time"].split())
    date_text = " ".join(match["date"].split())
    sender = match["sender"] or None
    return _parse_timestamp(time_text, date_text), sender


def _parse_timestamp(time_text: str, date_text: str) -> datetime | None:
    """Try every known time/date format combination; ``None`` if none fit."""
    for time_format in TIMESTAMP_TIME_FORMATS:
        for date_format in TIMESTAMP_DATE_FORMATS:
            try:
                return datetime.strptime(
                    f"{time_text} {date_text}", f"{time_format} {date_format}"
                )
            except ValueError:
                continue
    return None


def build_chat_message(raw_message: Mapping[str, Any]) -> ChatMessage:
    """Convert the dict extracted from a message element into a ChatMessage."""
    message_id = raw_message.get("dataId") or None
    timestamp, sender = parse_pre_plain_text(raw_message.get("prePlainText"))
    return ChatMessage(
        text=(raw_message.get("text") or "").strip(),
        direction=_detect_direction(
            message_id,
            raw_message.get("className") or "",
            has_outgoing_marker=bool(raw_message.get("hasOutgoingMarker")),
        ),
        timestamp=timestamp,
        sender=sender,
        message_id=message_id,
    )


def _detect_direction(
    message_id: str | None, class_name: str, has_outgoing_marker: bool = False
) -> MessageDirection:
    """Tell incoming from outgoing: own-message marker, data-id prefix, then CSS class."""
    if has_outgoing_marker:
        return MessageDirection.OUTGOING
    if message_id and message_id.startswith("true_"):
        return MessageDirection.OUTGOING
    if message_id and message_id.startswith("false_"):
        return MessageDirection.INCOMING
    if "message-out" in class_name:
        return MessageDirection.OUTGOING
    return MessageDirection.INCOMING


def build_unread_chat(raw_chat: Mapping[str, Any]) -> UnreadChat | None:
    """Convert a chat-row dict into an UnreadChat, or ``None`` if read/unnamed."""
    name = (raw_chat.get("name") or "").strip()
    if not name or not raw_chat.get("hasBadge"):
        return None
    count = _first_integer_in(raw_chat.get("badgeText"), raw_chat.get("badgeLabel"))
    # A badge without a number is the "marked as unread" dot: count it as one.
    return UnreadChat(name=name, unread_count=count if count is not None else 1)


def _first_integer_in(*candidates: str | None) -> int | None:
    """Return the first integer found in any candidate string."""
    for candidate in candidates:
        match = re.search(r"\d+", candidate or "")
        if match:
            return int(match.group())
    return None


def _message_key(message: ChatMessage) -> tuple[str, ...]:
    """Identity of a message: its id, or text+timestamp when it has no id."""
    if message.message_id:
        return ("id", message.message_id)
    return ("content", message.text, str(message.timestamp))


def _require_non_blank(value: str, field_name: str) -> None:
    """Raise ValueError unless ``value`` contains visible characters."""
    if not value or not value.strip():
        raise ValueError(f"{field_name} must not be empty")


class _Deadline:
    """Monotonic-clock deadline for polling loops."""

    def __init__(self, seconds: float) -> None:
        self._expires_at = time.monotonic() + seconds

    @property
    def is_expired(self) -> bool:
        """Return True once the allotted time has passed."""
        return time.monotonic() >= self._expires_at


# Runs in the browser; returns plain data so parsing stays testable in Python.
_EXTRACT_MESSAGE_SCRIPT = """
(element, args) => {
    const meta = element.querySelector('[data-pre-plain-text]');
    return {
        dataId: element.getAttribute('data-id'),
        className: element.className || '',
        hasOutgoingMarker: element.querySelector(args.outgoingMarker) !== null,
        text: Array.from(element.querySelectorAll(args.textSelector))
            .map((node) => node.innerText).join('\\n'),
        prePlainText: meta ? meta.getAttribute('data-pre-plain-text') : null,
    };
}
"""

_EXTRACT_CHAT_ROW_SCRIPT = """
(element, args) => {
    const title = element.querySelector(args.titleSelector);
    const badge = element.querySelector(args.badgeSelector);
    return {
        name: title ? (title.getAttribute('title') || title.innerText) : null,
        hasBadge: badge !== null,
        badgeText: badge ? badge.innerText : null,
        badgeLabel: badge ? badge.getAttribute('aria-label') : null,
    };
}
"""


# --------------------------------------------------------------------------
# Main class
# --------------------------------------------------------------------------
def _serialized(method: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    """Run an async client method while holding the client-wide lock.

    There is one browser page, so two public operations must never interleave
    (a send could otherwise be typed into another operation's chat).
    """

    @functools.wraps(method)
    async def wrapper(self: WhatsAppWeb, *args: Any, **kwargs: Any) -> T:
        async with self._lock:
            return await method(self, *args, **kwargs)

    return wrapper


@dataclass(frozen=True)
class SendPlan:
    """One guarded send: who, what, and how to prepare and deliver it."""

    recipients: tuple[str, ...]
    content: str | None
    prepare: Callable[[], Awaitable[None]]
    deliver: Callable[[], Awaitable[None]]
    creates_group: bool = False


# Failures that suggest the UI or account is misbehaving; they feed the breaker.
_SEND_FAILURES = (MessageSendError, ElementNotFoundError, GroupCreationError, PlaywrightError)


class WhatsAppWeb:
    """High-level, async automation of WhatsApp Web via Playwright (Chromium)."""

    def __init__(
        self,
        config: WhatsAppWebConfig | None = None,
        selectors: Selectors | None = None,
        controls: SendControls | None = None,
    ) -> None:
        """Create an unstarted client; call ``start()`` or use ``async with``."""
        self._config = config or WhatsAppWebConfig()
        self._selectors = selectors or Selectors()
        self._controls = controls or build_default_controls(self._config)
        self._was_logged_in = False
        self._lock = asyncio.Lock()
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    # ---- Lifecycle ---------------------------------------------------------
    async def __aenter__(self) -> WhatsAppWeb:
        """Start the browser and return this client."""
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the browser, whether or not the block raised."""
        await self.close()

    async def start(self) -> None:
        """Launch Chromium with a persistent profile. Idempotent."""
        if self._context is not None:
            logger.debug("start() called on an already started session")
            return
        self._playwright = await async_playwright().start()
        try:
            await self._open_browser(self._playwright)
        except BaseException:
            await self.close()
            raise
        logger.info("Browser started (headless=%s)", self._config.headless)

    async def _open_browser(self, playwright: Playwright) -> None:
        """Launch the context and prepare its page; callers clean up on error."""
        try:
            self._context = await self._launch_context(playwright)
        except Exception as error:
            raise BrowserLaunchError(f"Could not launch browser: {error}") from error
        self._page = await self._first_or_new_page(self._context)
        self._page.set_default_timeout(self._config.default_timeout_ms)

    async def close(self) -> None:
        """Release the browser context and Playwright. Safe to call repeatedly."""
        context, self._context = self._context, None
        self._page = None
        self._was_logged_in = False
        if context is not None:
            await self._log_and_ignore_errors("closing browser context", context.close())
        await self._stop_playwright()

    async def _launch_context(self, playwright: Playwright) -> BrowserContext:
        """Open the persistent Chromium context bound to the profile directory."""
        return await playwright.chromium.launch_persistent_context(
            str(self._config.user_data_dir),
            headless=self._config.headless,
            channel=self._config.channel,
        )

    @staticmethod
    async def _first_or_new_page(context: BrowserContext) -> Page:
        """Reuse the page Chromium opens with the profile, else create one."""
        if context.pages:
            return context.pages[0]
        return await context.new_page()

    async def _stop_playwright(self) -> None:
        """Stop the Playwright driver if it is running."""
        playwright, self._playwright = self._playwright, None
        if playwright is not None:
            await self._log_and_ignore_errors("stopping Playwright", playwright.stop())

    @staticmethod
    async def _log_and_ignore_errors(description: str, awaitable: Awaitable[Any]) -> None:
        """Await a cleanup step; log instead of raising so cleanup can continue."""
        try:
            await awaitable
        except Exception:
            logger.warning("Error while %s", description, exc_info=True)

    def _require_page(self) -> Page:
        """Return the active page or raise NotStartedError."""
        if self._page is None:
            raise NotStartedError("Call start() or use 'async with' before this operation")
        return self._page

    # ---- Public API --------------------------------------------------------
    @_serialized
    async def login(self) -> None:
        """Open WhatsApp Web and wait until the chat list is visible.

        If a QR code is shown it is logged, and the method waits up to
        ``login_timeout_s`` for it to be scanned (use headed mode).

        Raises:
            LoginTimeoutError: the chat list never appeared.
        """
        page = self._require_page()
        await self._retry(lambda: page.goto(WHATSAPP_WEB_URL, wait_until="domcontentloaded"))
        states = {"chat_list": self._selectors.chat_list, "qr": self._selectors.qr_code}
        deadline = _Deadline(self._config.login_timeout_s)
        is_qr_announced = False
        while True:
            state = await self._first_visible_state(states)
            if state == "chat_list":
                logger.info("Logged in: chat list is visible")
                self._was_logged_in = True
                return
            if state == "qr" and not is_qr_announced:
                self._announce_qr_code()
                is_qr_announced = True
            if deadline.is_expired:
                raise LoginTimeoutError(
                    f"Chat list not visible after {self._config.login_timeout_s:g}s; "
                    "was the QR code scanned?"
                )
            await asyncio.sleep(self._config.poll_interval_s)

    @_serialized
    async def send_message(self, contact_name: str, message: str) -> None:
        """Search for a contact or group, send ``message`` and confirm delivery.

        Raises:
            ContactNotFoundError: no chat matches ``contact_name``.
            MessageSendError: the message never showed up as sent.
            MessageUnconfirmedError: it showed up but was still pending.
            SafetyViolationError: a guardrail (opt-out, caps) blocked the send.
            AccountAtRiskError: the safety breaker is tripped.
        """
        _require_non_blank(contact_name, "contact_name")
        _require_non_blank(message, "message")
        plan = SendPlan(
            recipients=(recipient_key(contact_name),),
            content=message,
            prepare=lambda: self._open_chat_and_review(contact_name),
            deliver=lambda: self._deliver_text(message),
        )
        await self._run_send_pipeline(plan)
        logger.info("Message sent to %r", contact_name)

    @_serialized
    async def send_message_to_number(self, phone_number: str, message: str) -> None:
        """Send ``message`` to a phone number without needing a saved contact.

        Raises:
            InvalidPhoneNumberError: malformed number, or WhatsApp rejected it.
            SafetyViolationError: a guardrail (opt-out, caps) blocked the send.
        """
        normalized_number = normalize_phone_number(phone_number)
        _require_non_blank(message, "message")
        plan = SendPlan(
            recipients=(f"phone:{normalized_number}",),
            content=message,
            prepare=lambda: self._open_number_chat_and_review(normalized_number),
            deliver=lambda: self._deliver_text(message),
        )
        await self._run_send_pipeline(plan)
        logger.info("Message sent to +%s", normalized_number)

    @_serialized
    async def get_last_messages(self, contact_name: str, limit: int = 10) -> list[ChatMessage]:
        """Return up to ``limit`` most recent messages, oldest first."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        await self._open_chat(contact_name)
        await self._wait_for_message_rows()
        return await self._read_reviewed_messages(recipient_key(contact_name), limit)

    @_serialized
    async def get_unread_chats(self) -> list[UnreadChat]:
        """Return every chat in the list that shows an unread badge."""
        await self._ensure_session_active()
        await self._find_first(self._selectors.chat_list)
        rows = await self._locate_all(self._selectors.chat_rows)
        if rows is None:
            return []
        unread_chats: list[UnreadChat] = []
        for index in range(await rows.count()):
            unread_chat = await self._read_chat_row(rows.nth(index))
            if unread_chat is not None:
                unread_chats.append(unread_chat)
        return unread_chats

    @_serialized
    async def is_online(self, contact_name: str) -> bool:
        """Return True if the chat header shows "online" or "typing..."."""
        await self._open_chat(contact_name)
        indicator = await self._try_find_first(
            self._selectors.online_indicator, self._config.short_timeout_ms
        )
        return indicator is not None

    @_serialized
    async def wait_for_message(self, contact_name: str, timeout_s: float = 60.0) -> ChatMessage:
        """Wait for an incoming message newer than those already in the chat.

        The client stays locked meanwhile, so other operations queue up.

        Raises:
            MessageTimeoutError: nothing new arrived within ``timeout_s`` seconds.
        """
        recipient = recipient_key(contact_name)
        await self._open_chat(contact_name)
        await self._wait_for_message_rows()
        baseline_messages = await self._read_reviewed_messages(recipient, MESSAGE_SCAN_WINDOW)
        known_keys = {_message_key(message) for message in baseline_messages}
        deadline = _Deadline(timeout_s)
        while True:
            recent_messages = await self._read_reviewed_messages(recipient, MESSAGE_SCAN_WINDOW)
            new_message = self._first_new_incoming(recent_messages, known_keys)
            if new_message is not None:
                return new_message
            if deadline.is_expired:
                raise MessageTimeoutError(
                    f"No new message from {contact_name!r} within {timeout_s:g}s"
                )
            await asyncio.sleep(self._config.poll_interval_s)

    @_serialized
    async def send_file(
        self, contact_name: str, file_path: str | Path, caption: str = ""
    ) -> None:
        """Send an image, video or document, with an optional caption.

        Raises:
            AttachmentError: ``file_path`` does not exist or is not a file.
        """
        _require_non_blank(contact_name, "contact_name")
        path = self._validate_attachment(file_path)
        plan = SendPlan(
            recipients=(recipient_key(contact_name),),
            content=self._file_fingerprint(path, caption),
            prepare=lambda: self._open_chat_and_review(contact_name),
            deliver=lambda: self._deliver_file(path, caption),
        )
        await self._run_send_pipeline(plan)
        logger.info("File %s sent to %r", path.name, contact_name)

    @_serialized
    async def create_group(self, group_name: str, participants: Sequence[str]) -> None:
        """Create a group containing the given contacts.

        Counts against the rate limit, the daily group cap and the
        new-recipient cap, because every participant is contacted.

        Raises:
            ValueError: empty group name or participant list.
            ContactNotFoundError: a participant could not be found.
            GroupCreationError: the group did not open afterwards.
            SafetyViolationError: a guardrail blocked the creation.
        """
        participant_names = self._validate_group_arguments(group_name, participants)
        plan = SendPlan(
            recipients=tuple(recipient_key(name) for name in participant_names),
            content=None,
            prepare=self._prepare_group_creation,
            deliver=lambda: self._create_group_flow(group_name, participant_names),
            creates_group=True,
        )
        await self._run_send_pipeline(plan)
        logger.info("Group %r created with %d participants", group_name, len(participant_names))

    @_serialized
    async def logout(self) -> None:
        """Log out via the menu and wait for the QR screen to return."""
        self._require_page()
        await self._click_first(self._selectors.menu_button)
        await self._click_first(self._selectors.logout_item)
        confirm_button = await self._try_find_first(
            self._selectors.logout_confirm, self._config.short_timeout_ms
        )
        if confirm_button is not None:
            await confirm_button.click()
        qr_code = await self._try_find_first(
            self._selectors.qr_code, self._config.default_timeout_ms
        )
        if qr_code is None:
            raise LogoutError("QR code did not reappear after logging out")
        self._was_logged_in = False
        logger.info("Logged out")

    def opt_out(self, contact: str) -> None:
        """Refuse all future sends to this contact name or phone number."""
        self._controls.safety_guard.opt_out(recipient_key(contact))

    def opt_in(self, contact: str) -> None:
        """Allow sends to this contact again after an opt-out."""
        self._controls.safety_guard.opt_in(recipient_key(contact))

    def is_opted_out(self, contact: str) -> bool:
        """Return True if sends to this contact are refused."""
        return self._controls.safety_guard.is_opted_out(recipient_key(contact))

    def reset_safety_breaker(self) -> None:
        """Re-enable sending after investigating why the breaker tripped."""
        self._controls.safety_guard.reset_breaker()

    # ---- Element lookup helpers -------------------------------------------
    async def _try_find_first(
        self,
        candidates: Sequence[str],
        timeout_ms: int | None = None,
        state: str = "visible",
    ) -> Locator | None:
        """Return the best candidate reaching ``state``, or ``None``.

        All fallbacks race inside ONE wait of ``timeout_ms`` (a broken primary
        selector therefore costs no extra time); among the matches, the
        earliest candidate in the list wins.
        """
        if not candidates:
            raise ValueError("At least one selector candidate is required")
        total_timeout_ms = self._config.default_timeout_ms if timeout_ms is None else timeout_ms
        combined = self._combine_candidates(candidates)
        try:
            # Playwright treats a timeout of 0 as "wait forever"; keep it finite.
            await combined.first.wait_for(state=state, timeout=max(total_timeout_ms, 1))
        except PlaywrightTimeoutError:
            return None
        return await self._preferred_match(candidates, combined)

    def _combine_candidates(self, candidates: Sequence[str]) -> Locator:
        """Build one locator matching any candidate (``locator.or_`` chain)."""
        page = self._require_page()
        combined = page.locator(candidates[0])
        for selector in candidates[1:]:
            combined = combined.or_(page.locator(selector))
        return combined

    async def _preferred_match(self, candidates: Sequence[str], combined: Locator) -> Locator:
        """Return the first-listed candidate that matches, logging which one."""
        if len(candidates) == 1:
            return combined.first
        page = self._require_page()
        for selector in candidates:
            if await page.locator(selector).count() > 0:
                logger.debug("Matched selector: %s", selector)
                return page.locator(selector).first
        return combined.first

    async def _find_first(
        self,
        candidates: Sequence[str],
        timeout_ms: int | None = None,
        state: str = "visible",
    ) -> Locator:
        """Like ``_try_find_first`` but raises ElementNotFoundError on failure."""
        locator = await self._try_find_first(candidates, timeout_ms, state)
        if locator is None:
            raise ElementNotFoundError(f"No selector matched: {list(candidates)}")
        return locator

    async def _click_first(self, candidates: Sequence[str]) -> None:
        """Find the first matching element and click it."""
        element = await self._find_first(candidates)
        await element.click()

    async def _is_any_visible(self, candidates: Sequence[str]) -> bool:
        """Instantly check (no waiting) whether any candidate is visible."""
        page = self._require_page()
        for selector in candidates:
            if await page.locator(selector).first.is_visible():
                return True
        return False

    async def _locate_all(self, candidates: Sequence[str]) -> Locator | None:
        """Return a multi-element locator for the first candidate that has matches."""
        page = self._require_page()
        for selector in candidates:
            locator = page.locator(selector)
            if await locator.count() > 0:
                return locator
        return None

    async def _first_visible_state(self, states: Mapping[str, Sequence[str]]) -> str | None:
        """Return the name of the first state whose selectors are visible now."""
        for state_name, candidates in states.items():
            if await self._is_any_visible(candidates):
                return state_name
        return None

    async def _wait_for_state(
        self, states: Mapping[str, Sequence[str]], timeout_s: float
    ) -> str | None:
        """Poll until one named state is visible; ``None`` on timeout."""
        deadline = _Deadline(timeout_s)
        while True:
            state = await self._first_visible_state(states)
            if state is not None:
                return state
            if deadline.is_expired:
                return None
            await asyncio.sleep(self._config.poll_interval_s)

    async def _retry(
        self,
        operation: Callable[[], Awaitable[T]],
        retry_on: tuple[type[BaseException], ...] = (ElementNotFoundError, PlaywrightError),
    ) -> T:
        """Retry ``operation`` using the configured attempts and delay."""
        policy = RetryPolicy(
            attempts=self._config.retry_attempts,
            delay_seconds=self._config.retry_delay_s,
            retry_on=retry_on,
        )
        return await retry_async(operation, policy)

    # ---- Session and navigation -------------------------------------------
    def _announce_qr_code(self) -> None:
        """Tell the operator that a QR scan is required."""
        logger.info("QR code is displayed; scan it with your phone to log in")
        if self._config.headless:
            logger.warning("Running headless: the QR cannot be seen. Re-run with headless=False")

    async def _ensure_session_active(self) -> None:
        """Raise SessionExpiredError if WhatsApp is asking for a QR scan."""
        if await self._is_any_visible(self._selectors.qr_code):
            self._raise_session_lost()

    def _raise_session_lost(self) -> NoReturn:
        """Raise for a QR screen; an unexpected logout also trips the breaker."""
        if self._was_logged_in:
            reason = "session was logged out unexpectedly"
            self._controls.safety_guard.trip_breaker(reason)
            raise AccountAtRiskError(f"Sending halted: {reason}. Call reset_safety_breaker()")
        raise SessionExpiredError("WhatsApp shows a QR code; call login() and scan it")

    async def _check_account_health(self) -> None:
        """Raise if the breaker is tripped or a ban/spam banner is on screen."""
        self._controls.safety_guard.ensure_breaker_not_tripped()
        await self._raise_if_risk_warning()

    async def _raise_if_risk_warning(self) -> None:
        """Trip the breaker and raise if WhatsApp is showing a risk warning."""
        reason = await self._detect_risk_warning()
        if reason is not None:
            raise AccountAtRiskError(f"Sending halted: {reason}. Call reset_safety_breaker()")

    async def _detect_risk_warning(self) -> str | None:
        """Return a reason (after tripping the breaker) if a risk banner shows."""
        banner_text = await self._read_risk_banner_text()
        if banner_text is None:
            return None
        reason = f"WhatsApp warning detected: {' '.join(banner_text.split())[:120]}"
        self._controls.safety_guard.trip_breaker(reason)
        return reason

    async def _read_risk_banner_text(self) -> str | None:
        """Return the text of a visible risk banner, or None."""
        page = self._require_page()
        for selector in self._selectors.risk_banner:
            banner = page.locator(selector).first
            if not await banner.is_visible():
                continue
            banner_text = await banner.inner_text()
            if ACCOUNT_RISK_TEXT_PATTERN.search(banner_text):
                return banner_text
        return None

    async def _run_send_pipeline(self, plan: SendPlan) -> None:
        """Authorize, prepare, re-authorize, record and deliver one send.

        The rate-limit slot is taken BEFORE the chat is opened so a long wait
        cannot leave the page in the wrong chat, and every check runs again
        after the wait because the world may have changed meanwhile.
        """
        guard = self._controls.safety_guard
        await self._check_account_health()
        guard.precheck_send(plan.recipients, plan.content, plan.creates_group)
        await self._controls.rate_limiter.acquire()
        await plan.prepare()
        await self._check_account_health()
        guard.check_can_send(plan.recipients, plan.content, plan.creates_group)
        guard.record_send(plan.recipients, plan.content, plan.creates_group)
        await self._deliver_and_watch(plan.deliver)

    async def _deliver_and_watch(self, deliver: Callable[[], Awaitable[None]]) -> None:
        """Deliver, feed the outcome to the breaker, and look for warnings."""
        guard = self._controls.safety_guard
        try:
            await deliver()
        except _SEND_FAILURES:
            guard.record_failure()
            with contextlib.suppress(PlaywrightError):
                await self._raise_if_risk_warning()
            raise
        guard.record_success()
        with contextlib.suppress(PlaywrightError):
            await self._detect_risk_warning()

    async def _open_chat_and_review(self, contact_name: str) -> None:
        """Open a chat by name and learn from its history."""
        await self._open_chat(contact_name)
        await self._review_open_chat(recipient_key(contact_name))

    async def _open_number_chat_and_review(self, normalized_number: str) -> None:
        """Open a chat by number, link it to its title, and learn from history."""
        phone_key = f"phone:{normalized_number}"
        await self._open_chat_by_number(normalized_number)
        await self._link_number_to_chat_title(phone_key)
        await self._review_open_chat(phone_key)

    async def _review_open_chat(self, recipient: str) -> None:
        """Feed the open chat's history to the guard (known senders, opt-outs)."""
        await self._wait_for_message_rows()
        await self._read_reviewed_messages(recipient, MESSAGE_SCAN_WINDOW)

    async def _link_number_to_chat_title(self, phone_key: str) -> None:
        """Link the phone key to the chat's saved name so both share limits."""
        title_element = await self._try_find_first(
            self._selectors.chat_header_title, self._config.short_timeout_ms
        )
        if title_element is None:
            return
        title = (await title_element.inner_text()).strip()
        if not title:
            return
        title_key = recipient_key(title)
        if title_key != phone_key:
            self._controls.safety_guard.link_identities(phone_key, title_key)

    @staticmethod
    def _file_fingerprint(path: Path, caption: str) -> str:
        """Identify an upload by file content and caption for duplicate checks."""
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return f"file:{digest.hexdigest()}|{caption}"

    async def _deliver_text(self, message: str) -> None:
        """Type and send a text message, then confirm it left the outbox."""
        baseline = await self._snapshot_outgoing()
        await self._type_and_send(message)
        await self._verify_sent(baseline, self._config.send_verify_timeout_s)

    async def _deliver_file(self, path: Path, caption: str) -> None:
        """Attach, caption and send a file, then confirm the upload."""
        baseline = await self._snapshot_outgoing()
        await self._retry(lambda: self._attach_file(path), retry_on=(ElementNotFoundError,))
        if caption:
            await self._type_caption(caption)
        await self._click_media_send()
        await self._verify_sent(baseline, self._config.upload_verify_timeout_s)

    async def _read_reviewed_messages(self, recipient: str, limit: int) -> list[ChatMessage]:
        """Read recent messages and feed incoming ones to the safety guard."""
        messages = await self._read_recent_messages(limit)
        for message in messages:
            if message.direction is MessageDirection.INCOMING:
                message_id = "|".join(_message_key(message))
                self._controls.safety_guard.note_incoming(recipient, message_id, message.text)
        return messages

    async def _open_chat(self, contact_name: str) -> None:
        """Search for ``contact_name``, open the chat and wait for the composer."""
        _require_non_blank(contact_name, "contact_name")
        await self._ensure_session_active()
        await self._raise_if_risk_warning()
        await self._retry(
            lambda: self._search_and_select_chat(contact_name),
            retry_on=(ElementNotFoundError,),
        )
        await self._find_first(self._selectors.message_composer)

    async def _search_and_select_chat(self, contact_name: str) -> None:
        """Type the name in the search box and click the matching result."""
        search_box = await self._find_first(self._selectors.search_box)
        await search_box.click()
        await search_box.fill(contact_name)
        result_selectors = render_selector_templates(
            self._selectors.chat_result_templates, contact_name
        )
        chat_result = await self._try_find_first(result_selectors, self._config.short_timeout_ms)
        if chat_result is None:
            raise ContactNotFoundError(f"No chat found for {contact_name!r}")
        matching_chats = await self._count_matching_chats(result_selectors)
        if matching_chats > 1:
            await search_box.fill("")
            raise AmbiguousContactError(
                f"{matching_chats} chats are named {contact_name!r}; refusing to guess. "
                "Rename one of them or use send_message_to_number()"
            )
        await chat_result.click()
        # Leave the chat list unfiltered for later get_unread_chats() calls.
        await search_box.fill("")

    async def _count_matching_chats(self, result_selectors: Sequence[str]) -> int:
        """Count chat rows for the first result selector that matches any."""
        page = self._require_page()
        for selector in result_selectors:
            matches = await page.locator(selector).count()
            if matches:
                return matches
        return 0

    async def _open_chat_by_number(self, normalized_number: str) -> None:
        """Navigate to the click-to-chat URL and wait for it to resolve."""
        page = self._require_page()
        url = SEND_TO_NUMBER_URL_TEMPLATE.format(phone=normalized_number)
        await self._retry(lambda: page.goto(url, wait_until="domcontentloaded"))
        states = {
            "composer": self._selectors.message_composer,
            "invalid_number": self._selectors.modal_dialog,
            "qr": self._selectors.qr_code,
        }
        state = await self._wait_for_state(states, self._config.default_timeout_ms / 1000)
        if state == "composer":
            return
        if state == "invalid_number":
            # A ban or spam warning is a modal too: look before dismissing it.
            await self._raise_if_risk_warning()
            await self._dismiss_dialog()
            raise InvalidPhoneNumberError(f"WhatsApp reports +{normalized_number} as invalid")
        if state == "qr":
            self._raise_session_lost()
        raise ElementNotFoundError(f"Chat for +{normalized_number} did not open in time")

    async def _dismiss_dialog(self) -> None:
        """Click the first button of the open dialog, ignoring a missing button."""
        button = await self._try_find_first(
            self._selectors.modal_button, self._config.short_timeout_ms
        )
        if button is not None:
            await button.click()

    # ---- Sending -----------------------------------------------------------
    async def _type_and_send(self, text: str) -> None:
        """Type ``text`` into the composer and submit it."""
        composer = await self._find_first(self._selectors.message_composer)
        await composer.click()
        await self._clear_focused_text()
        await self._insert_multiline_text(text)
        send_button = await self._try_find_first(
            self._selectors.send_button, self._config.short_timeout_ms
        )
        if send_button is None:
            logger.debug("Send button not found; pressing Enter instead")
            await composer.press("Enter")
            return
        await send_button.click()

    async def _clear_focused_text(self) -> None:
        """Select all and delete, so a stale draft is never sent with the text."""
        page = self._require_page()
        await page.keyboard.press("ControlOrMeta+A")
        await page.keyboard.press("Backspace")

    async def _insert_multiline_text(self, text: str) -> None:
        """Insert text at the focused element; Shift+Enter makes line breaks.

        A plain Enter would send the message, so newlines are typed as
        Shift+Enter. With typing delay disabled, ``insert_text`` is used
        because it keeps emoji and non-Latin text intact and is instant.
        """
        page = self._require_page()
        normalized_text = text.replace("\r\n", "\n")
        delay_ms = self._controls.typing_delay.delay_ms_for(normalized_text)
        for index, line in enumerate(normalized_text.split("\n")):
            if index > 0:
                await page.keyboard.press("Shift+Enter")
            if not line:
                continue
            if delay_ms > 0:
                await page.keyboard.type(line, delay=delay_ms)
            else:
                await page.keyboard.insert_text(line)

    async def _snapshot_outgoing(self) -> _OutgoingSnapshot:
        """Capture the current newest outgoing message, without waiting."""
        outgoing = await self._locate_all(self._selectors.outgoing_messages)
        if outgoing is None:
            return _OutgoingSnapshot(count=0, last_message_id=None, is_pending=False)
        count = await outgoing.count()
        last_message = outgoing.nth(count - 1)
        pending_selector = ", ".join(self._selectors.pending_indicator)
        return _OutgoingSnapshot(
            count=count,
            last_message_id=await last_message.get_attribute("data-id"),
            is_pending=await last_message.locator(pending_selector).count() > 0,
        )

    async def _verify_sent(self, baseline: _OutgoingSnapshot, timeout_s: float) -> None:
        """Wait for a new outgoing message that has left the pending state.

        Raises:
            MessageUnconfirmedError: it appeared but stayed pending.
            MessageSendError: it never appeared.
        """
        deadline = _Deadline(timeout_s)
        has_appeared = False
        while True:
            current = await self._snapshot_outgoing()
            if not current.is_same_message_as(baseline):
                has_appeared = True
                if not current.is_pending:
                    return
            if deadline.is_expired:
                if has_appeared:
                    raise MessageUnconfirmedError(
                        f"Message still pending after {timeout_s:g}s; it may have been "
                        "delivered, so check the chat before retrying"
                    )
                raise MessageSendError(f"Message did not appear within {timeout_s:g}s")
            await asyncio.sleep(self._config.poll_interval_s)

    # ---- Reading messages --------------------------------------------------
    async def _wait_for_message_rows(self) -> None:
        """Give the conversation a moment to render; an empty chat is fine."""
        await self._try_find_first(self._selectors.message_rows, self._config.short_timeout_ms)

    async def _read_recent_messages(self, limit: int) -> list[ChatMessage]:
        """Parse the last ``limit`` rendered messages, oldest first."""
        rows = await self._locate_all(self._selectors.message_rows)
        if rows is None:
            return []
        total = await rows.count()
        indexes = range(max(0, total - limit), total)
        return [await self._parse_message_row(rows.nth(index)) for index in indexes]

    async def _parse_message_row(self, row: Locator) -> ChatMessage:
        """Extract and convert one message element."""
        script_args = {
            "textSelector": ", ".join(self._selectors.message_text),
            "outgoingMarker": ", ".join(self._selectors.outgoing_marker),
        }
        raw_message = await row.evaluate(_EXTRACT_MESSAGE_SCRIPT, script_args)
        return build_chat_message(raw_message)

    @staticmethod
    def _first_new_incoming(
        messages: Sequence[ChatMessage], known_keys: set[tuple[str, ...]]
    ) -> ChatMessage | None:
        """Return the oldest incoming message whose key is not yet known."""
        for message in messages:
            is_new = _message_key(message) not in known_keys
            if is_new and message.direction is MessageDirection.INCOMING:
                return message
        return None

    async def _read_chat_row(self, row: Locator) -> UnreadChat | None:
        """Extract one chat-list row and convert it if it is unread."""
        raw_chat = await row.evaluate(
            _EXTRACT_CHAT_ROW_SCRIPT,
            {
                "titleSelector": ", ".join(self._selectors.chat_title),
                "badgeSelector": ", ".join(self._selectors.unread_badge),
            },
        )
        return build_unread_chat(raw_chat)

    # ---- Attachments -------------------------------------------------------
    @staticmethod
    def _validate_attachment(file_path: str | Path) -> Path:
        """Return the resolved path or raise AttachmentError."""
        path = Path(file_path).expanduser()
        if not path.is_file():
            raise AttachmentError(f"File not found or not a regular file: {path}")
        return path

    @staticmethod
    def _is_image_or_video(path: Path) -> bool:
        """Return True for files WhatsApp sends as media rather than documents."""
        mime_type, _ = mimetypes.guess_type(path.name)
        return bool(mime_type) and mime_type.split("/")[0] in ("image", "video")

    async def _attach_file(self, path: Path) -> None:
        """Open the attach menu and hand the file to WhatsApp."""
        await self._click_first(self._selectors.attach_button)
        is_media = self._is_image_or_video(path)
        menu_item_selectors = (
            self._selectors.media_menu_item if is_media else self._selectors.document_menu_item
        )
        menu_item = await self._try_find_first(menu_item_selectors, self._config.short_timeout_ms)
        if menu_item is not None:
            await self._choose_file_via_menu(menu_item, path)
            return
        input_selectors = (
            self._selectors.media_file_input if is_media else self._selectors.document_file_input
        )
        # File inputs are hidden, so wait for presence rather than visibility.
        file_input = await self._find_first(input_selectors, state="attached")
        await file_input.set_input_files(str(path))

    async def _choose_file_via_menu(self, menu_item: Locator, path: Path) -> None:
        """Click an attach-menu entry and answer the file chooser it opens."""
        page = self._require_page()
        async with page.expect_file_chooser() as chooser_info:
            await menu_item.click()
        chooser = await chooser_info.value
        await chooser.set_files(str(path))

    async def _type_caption(self, caption: str) -> None:
        """Type the caption into the media preview."""
        caption_box = await self._find_first(self._selectors.caption_input)
        await caption_box.click()
        await self._insert_multiline_text(caption)

    async def _click_media_send(self) -> None:
        """Click the send button of the media preview."""
        await self._click_first(self._selectors.media_send_button)

    # ---- Groups ------------------------------------------------------------
    @staticmethod
    def _validate_group_arguments(group_name: str, participants: Sequence[str]) -> list[str]:
        """Validate inputs and return the cleaned participant names."""
        _require_non_blank(group_name, "group_name")
        if isinstance(participants, str) or not participants:
            raise ValueError("participants must be a non-empty list of names")
        for participant in participants:
            _require_non_blank(participant, "participant name")
        return [participant.strip() for participant in participants]

    async def _prepare_group_creation(self) -> None:
        """Make sure we are logged in, then open the new-group dialog."""
        await self._ensure_session_active()
        await self._click_first(self._selectors.new_chat_button)
        await self._click_first(self._selectors.new_group_item)

    async def _create_group_flow(self, group_name: str, participant_names: list[str]) -> None:
        """Pick participants, name the group and confirm it opened."""
        for participant_name in participant_names:
            await self._select_participant(participant_name)
        await self._click_first(self._selectors.group_next_button)
        await self._name_and_create_group(group_name)
        await self._verify_group_opened(group_name)

    async def _select_participant(self, participant_name: str) -> None:
        """Search for one participant in the group dialog and tick them."""
        search_box = await self._find_first(self._selectors.group_participant_search)
        await search_box.click()
        await search_box.fill(participant_name)
        result_selectors = render_selector_templates(
            self._selectors.participant_result_templates, participant_name
        )
        result = await self._try_find_first(result_selectors, self._config.short_timeout_ms)
        if result is None:
            raise ContactNotFoundError(f"Participant {participant_name!r} not found")
        await result.click()
        await search_box.fill("")

    async def _name_and_create_group(self, group_name: str) -> None:
        """Enter the subject and confirm creation."""
        name_box = await self._find_first(self._selectors.group_name_input)
        await name_box.click()
        await name_box.fill(group_name)
        await self._click_first(self._selectors.group_create_button)

    async def _verify_group_opened(self, group_name: str) -> None:
        """Confirm the new group's chat header is showing."""
        header_selectors = render_selector_templates(
            self._selectors.chat_header_title_templates, group_name
        )
        header = await self._try_find_first(header_selectors, self._config.default_timeout_ms)
        if header is None:
            raise GroupCreationError(f"Group {group_name!r} did not open after creation")


async def _example_main() -> None:  # pragma: no cover
    """Send one message using values from argv, the environment, or prompts."""
    import argparse
    import os

    parser = argparse.ArgumentParser(description="WhatsApp Web example")
    parser.add_argument("--contact", default=os.environ.get("WHATSAPP_CONTACT"))
    parser.add_argument("--message", default=os.environ.get("WHATSAPP_MESSAGE"))
    parser.add_argument("--headless", action="store_true", help="only after a first headed login")
    parser.add_argument("--user-data-dir", default=os.environ.get("WHATSAPP_USER_DATA_DIR"))
    args = parser.parse_args()

    contact = args.contact or input("Contact or group name: ")
    message = args.message or input("Message: ")
    config_options: dict[str, Any] = {"headless": args.headless}
    if args.user_data_dir:
        config_options["user_data_dir"] = args.user_data_dir

    async with WhatsAppWeb(WhatsAppWebConfig(**config_options)) as whatsapp:
        await whatsapp.login()
        await whatsapp.send_message(contact, message)


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(_example_main())
