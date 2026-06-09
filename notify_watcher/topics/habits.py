"""Topic: recurring daytime habit nudges, driven by habits.json.

A generic slot-reminder engine. Each habit in habits.json fires gentle nudges at
a set of daytime UTC hours, at most one push per slot per day, deduped in state.
No network and no secrets; the phrasing rotates per slot so a re-run on the same
slot never changes the text.

Robust against GitHub's dropped/delayed runs: a slot is "due" once its hour has
been reached, so a run that arrives a few minutes late still fires it. When runs
were skipped, a later run sends only the most recent due slot and marks the
earlier missed ones handled, so we never emit a burst of catch-up pings.

Adding a nudge (stand, stretch, eye-rest, ...) is a habits.json edit, not code:
give it a name/title/tag/hours/messages and flip ``enabled``. Edit the file
directly on github.com; no deploy needed.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
from pathlib import Path

from .. import kb, ntfy

log = logging.getLogger(__name__)

HABITS_PATH = Path(__file__).resolve().parent.parent.parent / "habits.json"


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _load() -> list[dict]:
    """Habit entries from habits.json; [] on a missing or malformed file."""
    try:
        data = json.loads(HABITS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.info("habits.json not found; nothing to do")
        return []
    except (OSError, json.JSONDecodeError) as exc:
        log.error("habits.json is not valid JSON: %s", exc)
        return []
    habits = data.get("habits") if isinstance(data, dict) else None
    return [h for h in habits if isinstance(h, dict)] if isinstance(habits, list) else []


def _state_key(name: str) -> str:
    # Keep water's historical key (water_slots_sent) so the migration is seamless.
    return f"{name}_slots_sent"


def _slot_key(day: _dt.date, hour: int) -> str:
    return f"{day.isoformat()}|{hour:02d}"


def _hours(habit: dict) -> list[int]:
    """Sorted, de-duped valid UTC hours (0-23) for a habit; [] if none/malformed."""
    out = {h for h in (habit.get("hours") or [])
           if isinstance(h, int) and not isinstance(h, bool) and 0 <= h < 24}
    return sorted(out)


def _due_slots(now: _dt.datetime, hours: list[int], sent: set[str]) -> list[int]:
    """Hours reached by `now` today and not yet sent, ascending."""
    today = now.date()
    return [h for h in hours if now.hour >= h and _slot_key(today, h) not in sent]


def _message_for(messages: list[str], day: _dt.date, hour: int) -> str:
    """Deterministic phrasing for a slot; staggered by hour so adjacent pings differ."""
    return kb.pick(messages, offset=hour, day=day) or messages[0]


def _run_one(state: dict, habit: dict, now: _dt.datetime) -> dict:
    """Process a single habit: send at most one due slot and update its state."""
    name = habit.get("name")
    if not habit.get("enabled", True):
        return state
    messages = [m for m in (habit.get("messages") or [])
                if isinstance(m, str) and m.strip()]
    hours = _hours(habit)
    if not name or not messages or not hours:
        log.warning("habit %r skipped: needs name, hours, and messages", name)
        return state

    today = now.date()
    skey = _state_key(name)
    sent = set(state.get(skey) or [])

    due = _due_slots(now, hours, sent)
    if due:
        latest = due[-1]  # ascending; send only the most recent slot
        ntfy.push(
            title=habit.get("title") or name,
            message=_message_for(messages, today, latest),
            tags=habit.get("tag") or "bell",
            priority="low",
        )
        log.info("habit %r: sent %02d:00 UTC slot", name, latest)
        for h in due:  # mark earlier missed slots handled too (no catch-up burst)
            sent.add(_slot_key(today, h))

    # Keep only today's keys so each habit's set stays small and resets daily.
    state[skey] = [k for k in sent if k.startswith(today.isoformat() + "|")]
    return state


def run(state: dict) -> dict:
    now = _utcnow()
    for habit in _load():
        try:
            state = _run_one(state, habit, now)
        except Exception as exc:  # noqa: BLE001 - one bad habit never blocks the rest
            log.error("habit %r failed: %s", habit.get("name"), exc)
    return state
