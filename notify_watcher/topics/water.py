"""Topic: one gentle "drink water" reminder per day.

A standing hydration nudge with no network and no secrets. The phrasing rotates
deterministically by day-of-year (see notify_watcher.kb), so the message varies
for freshness while a re-run on the same date always yields the same text - safe
against the runner's repeated/rebased runs.

Daily-only: this topic acts only when NOTIFY_DAILY is set (the daily cron) and is
further guarded by water_last_sent so a duplicate or drifted run never
double-sends. Pure local logic, so it never fails on a network hiccup.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os

from .. import kb, ntfy

log = logging.getLogger(__name__)

STATE_KEY = "water_last_sent"

# Curated phrasings rotated by day-of-year. Kept generic (no medical claims or
# numbers) so it stays a friendly nudge, not health advice.
_MESSAGES = [
    "Time for a glass of water. Stay hydrated!",
    "Hydration check: grab some water before you carry on.",
    "Quick reminder to drink some water.",
    "Keep a glass of water within reach today.",
    "Sip some water - your body will thank you.",
    "Pause for a moment and have a drink of water.",
    "Water break! A few sips now keeps you sharp.",
]


def _today() -> str:
    return _dt.date.today().isoformat()


def _message_for(day: _dt.date | None = None) -> str:
    """Deterministic day-of-year phrasing of the hydration nudge."""
    return kb.pick(_MESSAGES, day=day) or _MESSAGES[0]


def run(state: dict) -> dict:
    if not os.environ.get("NOTIFY_DAILY"):
        return state  # only the daily cron run sends the reminder
    if state.get(STATE_KEY) == _today():
        log.info("water reminder already sent today; skipping")
        return state

    ntfy.push(
        title="Drink water",
        message=_message_for(),
        tags="droplet",
        priority="low",
    )
    log.info("sent daily water reminder")
    state[STATE_KEY] = _today()
    return state
