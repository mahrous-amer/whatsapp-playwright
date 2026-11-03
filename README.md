# whatsapp-playwright

An async [Playwright](https://playwright.dev/python/) automation class for
WhatsApp Web, with persistent sessions, send pacing and account-health
guardrails. Everything lives in `whatsapp_web.py`.

> WhatsApp Web is a private UI that changes without notice, and automating it
> may violate WhatsApp's Terms of Service. Use it only for your own account and
> only to message people who expect to hear from you.

## Dependencies

- Python 3.10+ (developed on 3.15)
- `playwright` 1.45+ (runtime, see `requirements.txt`)
- `pytest`, `pytest-asyncio`, `pytest-cov` (development, see `requirements-dev.txt`)

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## First run (scan the QR code)

The browser profile is stored in a user-data directory (default
`~/.whatsapp-playwright/session`), so you only scan the QR code once.

```bash
# Headed mode is required the first time so you can see the QR code
python whatsapp_web.py --contact "Some Contact" --message "Hello"
```

Then open WhatsApp on your phone: Linked devices > Link a device, and scan.
Later runs reuse the saved session and can use `--headless`.

```python
import asyncio
from whatsapp_web import WhatsAppWeb, WhatsAppWebConfig

async def main():
    config = WhatsAppWebConfig(headless=False, user_data_dir="~/.wa-session")
    async with WhatsAppWeb(config) as wa:
        await wa.login()
        await wa.send_message("Some Contact", "Hello!")
        print(await wa.get_last_messages("Some Contact", limit=5))

asyncio.run(main())
```

One client drives one browser page, so every public operation holds an
internal `asyncio.Lock`: concurrent calls (for example from `asyncio.gather`)
run one after another, never interleaved. `wait_for_message(contact, timeout_s=60)`
keeps the client locked while it waits.

Main methods: `login`, `send_message`, `send_message_to_number`,
`get_last_messages`, `get_unread_chats`, `is_online`, `wait_for_message`,
`send_file`, `create_group`, `logout`, `close`.

All selectors live in the `Selectors` dataclass. If WhatsApp changes its DOM,
override just the broken fields and pass them in:
`WhatsAppWeb(config, selectors=dataclasses.replace(Selectors(), send_button=(...,)))`.

## Configuration

`WhatsAppWebConfig` options: `headless`, `user_data_dir`, `channel`
(for example `"chrome"` to use your installed browser instead of Playwright's
Chromium), `default_timeout_ms`, `short_timeout_ms`, `login_timeout_s`,
`send_verify_timeout_s`, `upload_verify_timeout_s`, `poll_interval_s`,
`retry_attempts`, `retry_delay_s`, plus the nested configs below.

### Send pacing

`RateLimitConfig` (all send paths acquire it before sending):

| Option | Default | Meaning |
| --- | --- | --- |
| `min_interval_seconds` | 3.0 | Minimum gap between sends |
| `max_per_minute` | 10 | Sliding 60 s window |
| `max_per_hour` | 60 | Sliding 1 h window |
| `max_per_day` | 200 | Sliding 24 h window; always raises `SafetyViolationError` |
| `on_limit` | `"wait"` | `"wait"` sleeps until allowed, `"raise"` raises `RateLimitExceededError` |

`TypingConfig` types messages with `keyboard.type(text, delay=...)`. The
per-character delay is drawn uniformly from `min_delay_ms`..`max_delay_ms`
(default 30..80), and `max_total_seconds` (default 10) caps the total typing
time of a message. Set both delays to 0 to disable (text is inserted instantly).

### How a send is processed

Every send path (`send_message`, `send_message_to_number`, `send_file`,
`create_group`) goes through the same pipeline: health check (breaker and
warning banners) and history-independent guard checks, then the rate-limit
slot (taken BEFORE any chat is opened, so a long wait cannot type into the
wrong chat), then open the chat and review its history, then all checks again,
record, deliver, and confirm. A send that is refused after the slot was taken
still consumes the slot.

Confirmation compares the id of the newest outgoing message. If the message
never appears you get `MessageSendError`; if it appeared but was still pending
at the timeout you get `MessageUnconfirmedError` (a subclass), which means it
**may have been delivered, so do not blindly retry**. The composer is cleared
(select all + Backspace) before typing so stale drafts are never sent.

### Avoiding bans

WhatsApp decides on bans mostly with server-side signals: how many people
block or report you, how many chats you start with people who never wrote to
you, and how repetitive your messages are. No client-side trick changes that,
and this project deliberately contains no stealth or anti-detection code.
Instead, `SafetyConfig` (on by default) keeps usage modest:

- **Rate limits**: conservative defaults above, including a daily cap.
- **New-recipient cap** (`max_new_recipients_per_day`, default 10): a recipient
  is "new" until they have sent you a message (seen in the chat history).
  Unknown numbers and group participants count as new.
- **Duplicate-broadcast guard** (`max_identical_recipients`, default 5): the
  same content cannot go to more than this many distinct recipients per day.
  Text is compared ignoring case and whitespace; for files the content is the
  SHA-256 of the file plus the caption (captions are optional, files are always
  compared). Only hashes are stored, never message text.
- **Group cap** (`max_groups_per_day`, default 2): `create_group` goes through
  the breaker, the rate limiter and the new-recipient accounting like any send.
- **Opt-outs** (`opt_out_keywords`, default `stop`, `unsubscribe`): an incoming
  message opts the sender out when, ignoring case, punctuation and emoji, it
  equals a keyword or starts with one ("STOP.", "Stop!", "stop 🙏",
  "stop sending me messages"; not "please stop" or "stopping"). It is checked
  whenever a chat is read (`get_last_messages`, `wait_for_message`, and before
  each send). Future sends raise `SafetyViolationError`. Manage manually with
  `opt_out()`, `opt_in()` and `is_opted_out()`. Names and phone numbers are
  different identities until linked: when `send_message_to_number` lands in a
  chat, the header title is read and the number is linked to the saved name, so
  an opt-out under either blocks both and counts are not doubled. If the
  header cannot be read, the two identities stay separate; opt out under both.
- **Circuit breaker** (`max_consecutive_failures`, default 3): an unexpected
  logout, a ban or spam warning banner (also checked when opening any chat,
  before dismissing the invalid-number dialog, and after each send), repeated
  send failures, an unreadable state file, or a state file that cannot be
  written halt all sends with `AccountAtRiskError` and a WARNING log, until you
  call `reset_safety_breaker()`. A warning appearing after a successful send
  trips the breaker without raising for that send. Banner detection uses
  English text patterns (`ACCOUNT_RISK_TEXT_PATTERN`) that may need updating.
- **Persistence**: counters, opt-outs, identity links, known senders and the
  breaker state are stored as JSON in `<user_data_dir>/safety_state.json`
  (override with `SafetyConfig(state_file=...)`). Writes are atomic (unique temp
  file, fsync, rename) and happen under an advisory file lock (`fcntl.flock`,
  skipped on platforms without it) as reload-merge-write, so two processes on
  one profile do not lose each other's updates. The system fails closed: an
  unreadable file is renamed to `safety_state.json.corrupt-<timestamp>` and the
  breaker starts tripped (`unreadable safety state`); a failed save trips the
  breaker in memory. Timestamps more than five minutes in the future are
  dropped on load, and a backwards wall-clock jump cannot bypass the windows.

Recommendations: message only people who expect it, honour every opt-out
request, keep volume low and human-like, and warm up new numbers slowly.
For bulk or business messaging use the official
[WhatsApp Business Cloud API](https://developers.facebook.com/docs/whatsapp/cloud-api)
instead of browser automation.

## Running tests

No browser or network is needed; Playwright is fully mocked.

```bash
pip install -r requirements-dev.txt
pytest --cov=whatsapp_web --cov-branch --cov-report=term-missing
```

Pytest, asyncio mode and coverage settings are in `pyproject.toml`.
