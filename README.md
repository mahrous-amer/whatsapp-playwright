# whatsapp-playwright

A small async Python wrapper around WhatsApp Web, built on Playwright. It logs
in once, keeps the session on disk, and lets you send and read messages from a
script. The whole thing is one file: `whatsapp_web.py`.

A word of warning before you use it: WhatsApp Web isn't a public API. The page
changes without notice, and automating it goes against WhatsApp's terms. I use
it for my own account and for people who expect to hear from me. If you need
volume or anything business-related, use the
[WhatsApp Business Cloud API](https://developers.facebook.com/docs/whatsapp/cloud-api).

## Setup

You need Python 3.10 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

If you'd rather use your installed Chrome, skip `playwright install` and pass
`channel="chrome"` in the config.

## Logging in the first time

Run it with a visible browser so you can scan the QR code:

```bash
python whatsapp_web.py --contact "Some Contact" --message "Hello"
```

On your phone go to Settings > Linked devices > Link a device and scan the code.
The session is saved in `~/.whatsapp-playwright/session`, so after that you
won't see the QR code again and can add `--headless`.

## Using it

```python
import asyncio
from whatsapp_web import WhatsAppWeb, WhatsAppWebConfig

async def main():
    config = WhatsAppWebConfig(channel="chrome")
    async with WhatsAppWeb(config) as wa:
        await wa.login()
        await wa.send_message("Some Contact", "Hello!")
        for message in await wa.get_last_messages("Some Contact", limit=5):
            print(message.direction, message.text)

asyncio.run(main())
```

What's there:

- `login()`
- `send_message(name, text)` and `send_message_to_number(number, text)`
- `send_file(name, path, caption="")`
- `get_last_messages(name, limit=10)` and `wait_for_message(name, timeout_s=60)`
- `get_unread_chats()` and `is_online(name)`
- `create_group(name, participants)`
- `logout()` and `close()`

Chats are found by their exact name. If two chats have the same name (say a
contact who shares your own name, next to your "Message yourself" chat), you
get `AmbiguousContactError` and nothing is opened or sent. Rename one of them,
or use `send_message_to_number`.

One client drives one browser tab, so calls are queued behind a lock. If you
`asyncio.gather` two sends, they run one after the other.

Sends are checked: after sending, the client waits for the message to show up
in the chat and leave the "pending" state. If it never shows up you get
`MessageSendError`. If it shows up but is still pending when the timeout hits,
you get `MessageUnconfirmedError`, which means it may well have gone through,
so look before you retry. Nothing is retried automatically, to avoid double
sends.

### When WhatsApp changes its page

It will. All the CSS selectors are in the `Selectors` dataclass, each with a
few fallbacks. When something breaks, fix the one field rather than the code:

```python
from dataclasses import replace
from whatsapp_web import Selectors

selectors = replace(Selectors(), send_button=('button[aria-label="Send"]',))
wa = WhatsAppWeb(config, selectors=selectors)
```

The selectors were last checked against the live site in November 2026. Some
of them match English text (the "You:" and "Read" labels on your own messages,
menu names like "Document"), so a WhatsApp set to another language will need
overrides.

## Configuration

`WhatsAppWebConfig` takes `headless`, `user_data_dir`, `channel`, a handful of
timeouts (`default_timeout_ms`, `login_timeout_s`, `send_verify_timeout_s`,
...), retry settings, and three nested configs for pacing and safety.

### Pacing

`RateLimitConfig` spaces out sends:

| Option | Default | |
| --- | --- | --- |
| `min_interval_seconds` | 3 | gap between two sends |
| `max_per_minute` | 10 | |
| `max_per_hour` | 60 | |
| `max_per_day` | 200 | hitting this raises instead of waiting |
| `on_limit` | `"wait"` | or `"raise"` to get `RateLimitExceededError` |

`TypingConfig` types messages a character at a time, 30 to 80 ms per character,
but never for more than 10 seconds per message. Set both delays to 0 to paste
the text in at once.

### Keeping your account safe

WhatsApp bans accounts mostly for what happens on its side: people blocking or
reporting you, lots of chats started with strangers, the same text sent to
everyone. A browser script can't hide that, so there's no "stealth" code here.
`SafetyConfig` just keeps you from doing those things by accident. It's on by
default:

- At most 10 messages a day to people who have never written to you
  (`max_new_recipients_per_day`). New numbers and group members count.
- The same text, or the same file, to at most 5 people a day
  (`max_identical_recipients`). Only hashes are stored, not the messages.
- At most 2 new groups a day (`max_groups_per_day`).
- If someone replies "stop" or "unsubscribe" (punctuation and emoji are
  ignored, so "STOP." works), they're opted out and further sends to them
  raise `SafetyViolationError`. You can also call `opt_out()`, `opt_in()` and
  `is_opted_out()` yourself.
- If WhatsApp logs you out unexpectedly, shows a spam or ban warning, or three
  sends fail in a row, all sending stops with `AccountAtRiskError` until you
  look into it and call `reset_safety_breaker()`.

All of this is saved in `safety_state.json` inside the session folder, so
restarting the script doesn't reset the counters. If that file gets corrupted,
it's moved aside and sending stays blocked until you reset it, rather than
quietly starting from zero.

A contact you message by name and by number counts as two people unless the
client can match them up (it reads the chat header to do that). If you opt
someone out by hand, do it for both.

## Tests

The tests mock Playwright, so they don't need a browser or a WhatsApp account:

```bash
pip install -r requirements-dev.txt
pytest --cov=whatsapp_web --cov-branch --cov-report=term-missing
```

Coverage is at 100%, but that only proves the logic. The mocks don't know what
the real page looks like, so after changing selectors, try it against your own
"Message yourself" chat.

## Not tried against the live site yet

`send_message_to_number`, `wait_for_message` and `create_group` pass their unit
tests but haven't been run against real WhatsApp. Expect to adjust a selector
or two. Also, documents you send come back from `get_last_messages` without a
timestamp.
