"""Topic: periodic "drink water" reminders through the day.

Several gentle hydration nudges spread across daytime hours, instead of a single
daily push. The runner fires every 3 hours (see the workflow cron), so we map a
set of daytime UTC hours to reminder slots and send at most one push per slot
per day, deduped in state. No network and no secrets; phrasing rotates
deterministically so a re-run on the same slot never changes the text.

Robust against GitHub's dropped/delayed runs: a slot is "due" once its hour has
been reached, so a run that arrives a few minutes late still fires it. When runs
were skipped, a later run sends only the most recent due slot and marks the
earlier missed ones as handled, so we never emit a burst of catch-up pings.
"""
from __future__ import annotations

import datetime as _dt
import logging

from .. import kb, ntfy

log = logging.getLogger(__name__)

STATE_KEY = "water_slots_sent"

# Daytime reminder slots as UTC hours, aligned to the every-3-hours cron grid.
# 12/15/18/21 UTC == 08:00/11:00/14:00/17:00 in the Dominican Republic (UTC-4),
# i.e. morning through late afternoon. Ascending and within a single day (no
# midnight wrap), so "due once its hour is reached" stays simple.
REMINDER_UTC_HOURS = [12, 15, 18, 21]

# Curated phrasings rotated by slot. Kept generic (no medical claims or numbers)
# so it stays a friendly nudge, not health advice.
_MESSAGES = [
    "Time for a glass of water. Stay hydrated!",
    "Hydration check: grab some water before you carry on.",
    "Quick reminder to drink some water.",
    "Keep a glass of water within reach.",
    "Sip some water - your body will thank you.",
    "Pause for a moment and have a drink of water.",
    "Water break! A few sips now keeps you sharp.",
]


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _slot_key(day: _dt.date, hour: int) -> str:
    return f"{day.isoformat()}|{hour:02d}"


def _due_slots(now: _dt.datetime, sent: set[str]) -> list[int]:
    """Reminder hours reached by `now` today and not yet sent, ascending."""
    today = now.date()
    return [h for h in REMINDER_UTC_HOURS
            if now.hour >= h and _slot_key(today, h) not in sent]


def _message_for(day: _dt.date, hour: int) -> str:
    """Deterministic phrasing for a slot; staggered by hour so adjacent pings differ."""
    return kb.pick(_MESSAGES, offset=hour, day=day) or _MESSAGES[0]


def run(state: dict) -> dict:
    now = _utcnow()
    today = now.date()
    sent = set(state.get(STATE_KEY) or [])

    due = _due_slots(now, sent)
    if due:
        latest = due[-1]  # ascending; send only the most recent slot
        ntfy.push(
            title="Drink water",
            message=_message_for(today, latest),
            tags="droplet",
            priority="low",
        )
        log.info("sent water reminder for %02d:00 UTC slot", latest)
        for h in due:  # mark any earlier missed slots handled too (no catch-up burst)
            sent.add(_slot_key(today, h))

    # Keep only today's keys so the set stays small (<= len(REMINDER_UTC_HOURS))
    # and each new day starts fresh.
    state[STATE_KEY] = [k for k in sent if k.startswith(today.isoformat() + "|")]
    return state
