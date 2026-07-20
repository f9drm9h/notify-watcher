"""Topic: daily habit tracker — scheduled reminders + reaction-based completion.

habits.json defines each habit's reminder slots in LOCAL Dominican Republic
time ("HH:MM", UTC + ``utc_offset_hours``; DR is -4 year-round, no DST). This
topic rides the 15-minute fast lane (watch.yml runs ``twitch,habits`` between
full sweeps), so a slot fires on the first run at or after its time — within
~15 minutes of schedule. At most one push per slot per day, deduped in state;
when runs were skipped, only the most recent due slot is sent and earlier
missed ones are marked handled, so there is never a catch-up burst.

Reminders are user-scheduled, so they bypass the priority engine on purpose
(same rationale as ``control.process_pending``: the user explicitly asked for
each one, so it must not be digested or dropped by routing) and are pushed
directly to the habits Discord channel. An active MUTE:habits still silences
them (the slot is marked handled — a time-critical nudge is useless tomorrow).
Each push is recorded in the event log so the dashboard and the weekly life
dashboard keep their habit history.

Completion is reaction-based: after posting, the bot adds a ✅ reaction to its
own message and remembers the message id in ``state["habit_pending_acks"]``.
Every run first polls those messages; any reaction beyond the bot's own counts
as an acknowledgment and calls :func:`mark_complete`, which appends
``{habit, date, completed_at}`` to ``state["habit_log"]`` (capped, timestamps
in UTC) and silences the habit's remaining slots that day — acking the 9 PM
sleep wind-down also cancels the 10 PM one. The reminder's [Done] button goes
through ``control.cmd_done`` into the very same :func:`mark_complete`, so both
paths are idempotent: one completion per habit per local day. Unacked pending
entries expire quietly after ``PENDING_TTL_HOURS``; there is no weekly summary.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

import requests

from .. import config, control, discord_delivery, eventlog, events, health, kb, ntfy

log = logging.getLogger(__name__)

HABITS_PATH = Path(__file__).resolve().parent.parent.parent / "habits.json"

PENDING_KEY = "habit_pending_acks"  # {message_id: {habit, channel, sent}}
LOG_KEY = "habit_log"               # [{habit, date, completed_at}], FIFO-capped
MAX_LOG = 1000
MAX_PENDING = 40
PENDING_TTL_HOURS = 24.0
ACK_EMOJI = "✅"

DEFAULT_UTC_OFFSET = -4  # Dominican Republic, no DST
_TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _load_file() -> dict:
    """The parsed habits.json dict; {} on a missing or malformed file."""
    try:
        data = json.loads(HABITS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.info("habits.json not found; nothing to do")
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        log.error("habits.json is not valid JSON: %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


def _load() -> list[dict]:
    """Habit entries from habits.json; [] on a missing or malformed file."""
    habits = _load_file().get("habits")
    return [h for h in habits if isinstance(h, dict)] if isinstance(habits, list) else []


def _utc_offset() -> int:
    """Configured local-time offset in hours, clamped to a real-world range."""
    offset = _load_file().get("utc_offset_hours", DEFAULT_UTC_OFFSET)
    if not isinstance(offset, int) or isinstance(offset, bool):
        return DEFAULT_UTC_OFFSET
    return max(-12, min(14, offset))


def _local_now(now: _dt.datetime) -> _dt.datetime:
    """The configured local wall-clock time for a UTC instant."""
    return now + _dt.timedelta(hours=_utc_offset())


def _state_key(name: str) -> str:
    # Keep the historical per-habit key shape (water_slots_sent) across formats.
    return f"{name}_slots_sent"


def _slot_key(day: _dt.date, hhmm: str) -> str:
    return f"{day.isoformat()}|{hhmm}"


def _times(habit: dict) -> list[str]:
    """Sorted, de-duped valid local 'HH:MM' times for a habit; [] if malformed."""
    out = {t for t in (habit.get("times") or [])
           if isinstance(t, str) and _TIME_RE.match(t)}
    return sorted(out)


def _due_slots(local_now: _dt.datetime, times: list[str], sent: set[str]) -> list[str]:
    """Times reached by `local_now` today and not yet sent, ascending.

    Zero-padded 'HH:MM' strings compare correctly as strings, so this is a
    plain lexicographic filter against the current local wall clock.
    """
    today = local_now.date()
    now_hhmm = local_now.strftime("%H:%M")
    return [t for t in times if t <= now_hhmm and _slot_key(today, t) not in sent]


def _minutes(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


def _message_for(messages: list[str], day: _dt.date, hhmm: str) -> str:
    """Deterministic phrasing per slot; offset by minute so adjacent slots differ."""
    return kb.pick(messages, offset=_minutes(hhmm), day=day) or messages[0]


def _muted(state: dict) -> bool:
    try:
        return control.until_active((state.get("muted") or {}).get("habits"))
    except Exception:  # noqa: BLE001 - mute check must never block a reminder
        return False


def _record_event(state: dict, habit: dict, body: str, now: _dt.datetime) -> None:
    """Append the sent reminder to the event log (dashboard + life-dashboard
    history). Best-effort: a logging failure never blocks the reminder flow."""
    try:
        cfg = config.section("priority")
    except Exception:  # noqa: BLE001
        cfg = None
    try:
        event = events.Event(
            title=habit.get("title") or str(habit.get("name")),
            body=body,
            topic="habits",
            severity="low",
            source=str(habit.get("name")),
            timestamp=now.isoformat(),
        )
        eventlog.record(state, event, "push", 0, cfg)
    except Exception as exc:  # noqa: BLE001
        log.warning("habits: event-log record failed: %s", exc)


def _register_pending(state: dict, message_id: str, name: str,
                      now: _dt.datetime) -> None:
    """Remember a delivered reminder so later runs can poll it for a ✅ ack."""
    channel = discord_delivery.channel_for("habits")
    if not channel:
        return
    pending = state.setdefault(PENDING_KEY, {})
    pending[str(message_id)] = {
        "habit": name,
        "channel": channel,
        "sent": now.isoformat(),
    }
    while len(pending) > MAX_PENDING:
        pending.pop(next(iter(pending)))


def _seed_ack_reaction(message_id: str) -> None:
    """Pre-add the ✅ so acknowledging is one tap. Failure is cosmetic."""
    channel = discord_delivery.channel_for("habits")
    if not channel:
        return
    try:
        discord_delivery.add_reaction(channel, message_id, ACK_EMOJI)
    except Exception as exc:  # noqa: BLE001 - the user can react without the seed
        log.warning("habits: could not pre-add %s reaction: %s", ACK_EMOJI, exc)


def _completed_today(state: dict, name: str, day: _dt.date) -> bool:
    return any(isinstance(e, dict) and e.get("habit") == name
               and e.get("date") == day.isoformat()
               for e in (state.get(LOG_KEY) or []))


def _suppress_today(state: dict, name: str, local_now: _dt.datetime) -> None:
    """Mark every one of the habit's slots today as handled (done for the day)."""
    habit = next((h for h in _load() if h.get("name") == name), None)
    if habit is None:
        return
    skey = _state_key(name)
    sent = set(state.get(skey) or [])
    for t in _times(habit):
        sent.add(_slot_key(local_now.date(), t))
    state[skey] = sorted(sent)


def _drop_pending(state: dict, name: str) -> None:
    pending = state.get(PENDING_KEY) or {}
    for mid in [m for m, e in pending.items()
                if isinstance(e, dict) and e.get("habit") == name]:
        pending.pop(mid, None)


def mark_complete(state: dict, name: str,
                  now: Optional[_dt.datetime] = None) -> bool:
    """Log a habit completion and silence its remaining reminders today.

    The single completion handler behind both ack paths (✅ reaction poll and
    the [Done] button via ``control.cmd_done``). Appends ``{habit, date,
    completed_at}`` to ``state["habit_log"]`` — ``date`` is the LOCAL habit
    day, ``completed_at`` a UTC timestamp — capped at MAX_LOG. Idempotent:
    one completion per habit per local day; a repeat still clears pending
    acks and slot suppression but returns False without a duplicate entry.
    """
    now = now or _utcnow()
    local = _local_now(now)
    _drop_pending(state, name)
    _suppress_today(state, name, local)
    if _completed_today(state, name, local.date()):
        return False
    entries = state.setdefault(LOG_KEY, [])
    entries.append({
        "habit": name,
        "date": local.date().isoformat(),
        "completed_at": now.isoformat(),
    })
    if len(entries) > MAX_LOG:
        del entries[: len(entries) - MAX_LOG]
    log.info("habits: %r completed for %s", name, local.date().isoformat())
    return True


def _acked(message: Optional[dict]) -> bool:
    """True when anyone besides the bot reacted to the reminder message.

    Discord's message JSON lists each emoji with a total ``count`` and ``me``
    (did the bot itself react). Any count beyond the bot's own reaction — a
    tap on the seeded ✅ or any other emoji — is an acknowledgment.
    """
    if not isinstance(message, dict):
        return False
    for reaction in message.get("reactions") or []:
        if not isinstance(reaction, dict):
            continue
        try:
            count = int(reaction.get("count") or 0)
        except (TypeError, ValueError):
            continue
        if count - (1 if reaction.get("me") else 0) > 0:
            return True
    return False


def _expired(entry: dict, now: _dt.datetime) -> bool:
    try:
        sent = _dt.datetime.fromisoformat(str(entry.get("sent")))
    except ValueError:
        return True  # unparseable timestamp: drop rather than poll forever
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=_dt.timezone.utc)
    return now - sent > _dt.timedelta(hours=PENDING_TTL_HOURS)


def _poll_acks(state: dict, now: _dt.datetime) -> None:
    """Check pending reminder messages for reaction acks; log completions.

    One REST GET per still-pending message (a handful a day at most). Network
    errors keep the entry for a retry next run; a 404 (message deleted) drops
    it; entries older than PENDING_TTL_HOURS expire quietly — the habit simply
    goes unlogged that day.
    """
    pending = state.get(PENDING_KEY)
    if not isinstance(pending, dict) or not pending:
        return
    if not (os.getenv("DISCORD_TOKEN") or "").strip():
        return  # offline/local: nothing to poll, keep entries for a real run
    for message_id, entry in list(pending.items()):
        if not isinstance(entry, dict) or _expired(entry, now):
            pending.pop(message_id, None)
            continue
        try:
            message = discord_delivery.get_message(
                str(entry.get("channel")), message_id)
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status == 404:
                pending.pop(message_id, None)
                log.info("habits: reminder message %s is gone; dropped", message_id)
            else:
                log.warning("habits: ack poll for %s failed (will retry): %s",
                            message_id, exc)
            continue
        except Exception as exc:  # noqa: BLE001 - poll must never block the run
            log.warning("habits: ack poll for %s failed (will retry): %s",
                        message_id, exc)
            continue
        if _acked(message):
            mark_complete(state, str(entry.get("habit")), now=now)


def _run_one(state: dict, habit: dict, now: _dt.datetime) -> dict:
    """Process a single habit: send at most one due slot and update its state."""
    name = habit.get("name")
    if not habit.get("enabled", True):
        return state
    messages = [m for m in (habit.get("messages") or [])
                if isinstance(m, str) and m.strip()]
    times = _times(habit)
    if not name or not messages or not times:
        log.warning("habit %r skipped: needs name, times, and messages", name)
        return state

    local = _local_now(now)
    today = local.date()
    skey = _state_key(name)
    sent = set(state.get(skey) or [])

    due = _due_slots(local, times, sent)
    if due:
        latest = due[-1]  # ascending; send only the most recent slot
        if _muted(state):
            log.info("habit %r: muted; skipping the %s slot", name, latest)
        else:
            body = _message_for(messages, today, latest)
            done = control.make_action("Done", f"DONE:{name}")
            message_id = ntfy.push(
                title=habit.get("title") or name,
                message=body,
                tags=habit.get("tag") or "bell",
                priority=habit.get("priority") or "default",
                topic="habits",
                severity="low",
                **({"actions": [done]} if done else {}),
            )
            _record_event(state, habit, body, now)
            if message_id:
                _register_pending(state, message_id, name, now)
                _seed_ack_reaction(message_id)
            log.info("habit %r: sent the %s slot", name, latest)
        for t in due:  # mark earlier missed slots handled too (no catch-up burst)
            sent.add(_slot_key(today, t))

    # Keep only today's (local) keys so each habit's set stays small and resets daily.
    state[skey] = sorted(k for k in sent if k.startswith(today.isoformat() + "|"))
    return state


def run(state: dict) -> dict:
    now = _utcnow()
    try:
        _poll_acks(state, now)  # first, so a fresh ack silences this run's sends
    except Exception as exc:  # noqa: BLE001 - ack polling never blocks reminders
        log.warning("habits: ack polling failed: %s", exc)
    habits = _load()
    failures = 0
    last_error = ""
    for habit in habits:
        try:
            state = _run_one(state, habit, now)
        except Exception as exc:  # noqa: BLE001 - one bad habit never blocks the rest
            log.error("habit %r failed: %s", habit.get("name"), exc)
            failures += 1
            last_error = f"{habit.get('name')}: {exc}"
    # Health contract (local source: habits.json): ok while at least one habit
    # processed cleanly; source_failed when every configured habit errored —
    # before this, a bug breaking all habits still stamped a healthy last_ok
    # every run. No habits configured = no claim.
    if habits:
        if failures < len(habits):
            health.source_ok(state, "habits", data_count=len(habits) - failures)
        else:
            health.source_failed(
                state, "habits",
                f"all {len(habits)} habit(s) failed; last: {last_error}")
    return state
