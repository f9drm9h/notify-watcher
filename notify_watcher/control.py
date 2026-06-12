"""Two-way control channel: poll a private ntfy topic for reply-button commands.

Notification action buttons (ntfy `http` actions) POST a small command string —
``DONE:water``, ``SNOOZE:passport:60``, ``MUTE:movies:24`` — to a second private
ntfy topic (``NTFY_CONTROL_TOPIC``). ntfy's server-side message cache (~12 h) is
the queue; ``poll`` drains it at the top of every run and ``dispatch`` routes
each command to a handler that mutates state. The topics then run against the
mutated state, so a command takes effect in the same run that reads it.

Kill switch: an unset/empty ``NTFY_CONTROL_TOPIC`` disables everything — ``poll``
returns [] immediately and ``make_action`` returns None so no buttons are
attached, leaving push behavior byte-identical to a build without this module.

Every command is idempotent and bounded (durations clamped, per-poll cap,
strict per-verb regexes that fail closed on anything unknown), so a replayed,
duplicated, or hostile command can suppress a nudge or mute a digest topic for
a while but never corrupt state, read data, or execute code. See
docs/design/reply-buttons.md for the full design and threat model.

State keys owned by this module:
  control : {"last_id": str}   poll cursor (newest processed ntfy message id)
  snoozed : {reminder_id: until_iso}   read by topics/reminders.py
  muted   : {topic: until_iso}         read by events.emit
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
from typing import Optional

import requests

log = logging.getLogger(__name__)

DEFAULT_SERVER = "https://ntfy.sh"
STATE_KEY = "control"
SNOOZED_KEY = "snoozed"
MUTED_KEY = "muted"

# Clamps bound every command's effect; a malformed or hostile duration can
# never snooze/mute longer than 30 days. At most MAX_PER_POLL commands are
# processed per run so a flooded control topic can't stall the watch run.
MIN_SNOOZE_MINUTES, MAX_SNOOZE_MINUTES = 5, 43_200  # 5 min .. 30 d
MIN_MUTE_HOURS, MAX_MUTE_HOURS = 1, 720             # 1 h .. 30 d
MAX_PER_POLL = 50

# Strict per-verb grammar: ASCII slug ids, numeric durations. Anything that
# doesn't match a verb exactly is logged and dropped (fail closed).
_DONE_RE = re.compile(r"^DONE:([A-Za-z0-9_-]+)$")
_SNOOZE_RE = re.compile(r"^SNOOZE:([A-Za-z0-9_-]+):(\d{1,6})$")
_MUTE_RE = re.compile(r"^MUTE:([A-Za-z0-9_-]+):(\d{1,4})$")


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _topic() -> str:
    return os.environ.get("NTFY_CONTROL_TOPIC", "").strip()


def _server() -> str:
    return (os.environ.get("NTFY_SERVER", "").strip() or DEFAULT_SERVER).rstrip("/")


def make_action(label: str, command: str) -> Optional[dict]:
    """One ntfy ``http`` action button that POSTs `command` to the control topic.

    Returns None when NTFY_CONTROL_TOPIC is unset (feature off), so callers can
    build their button list unconditionally and attach metadata only when it is
    non-empty — pushes stay byte-identical with the feature disabled.

    ``clear: true`` dismisses the notification once the tap's POST succeeds,
    which doubles as the delivery ack (no confirmation push is sent).
    """
    topic = _topic()
    if not topic:
        return None
    return {
        "action": "http",
        "label": label,
        "url": f"{_server()}/{topic}",
        "method": "POST",
        "body": command,
        "clear": True,
    }


def until_active(until_iso: object, now: Optional[_dt.datetime] = None) -> bool:
    """True if an ISO 'until' timestamp lies in the future.

    Missing/malformed values return False (fail open: an unparseable mute or
    snooze never suppresses anything). Naive timestamps are assumed UTC.
    """
    if not isinstance(until_iso, str):
        return False
    try:
        until = _dt.datetime.fromisoformat(until_iso)
    except ValueError:
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=_dt.timezone.utc)
    return until > (now or _utcnow())


def poll(state: dict) -> list[str]:
    """Fetch new command strings from the control topic; [] when disabled.

    One cheap GET against ntfy's poll endpoint (``poll=1`` returns the cached
    backlog and closes). The cursor is the newest seen message id, kept in
    state["control"]["last_id"] and passed back as ``since=<id>``; the first
    ever poll uses ``since=all``, harmless because the cache only holds ~12 h
    and every command is idempotent. Network errors are logged and yield []
    without advancing the cursor, so the batch is re-read next run.
    """
    topic = _topic()
    if not topic:
        return []
    ctl = state.setdefault(STATE_KEY, {})
    since = ctl.get("last_id") or "all"
    try:
        resp = requests.get(
            f"{_server()}/{topic}/json",
            params={"poll": "1", "since": since},
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.text
    except Exception as exc:  # noqa: BLE001 - control must never block the run
        log.warning("control poll failed (will retry next run): %s", exc)
        return []

    commands: list[str] = []
    last_id = None
    for line in body.splitlines():  # ndjson: one event object per line
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict) or msg.get("event") != "message":
            continue
        if msg.get("id"):
            last_id = msg["id"]
        text = msg.get("message")
        if isinstance(text, str) and text.strip():
            commands.append(text.strip())

    if last_id:
        ctl["last_id"] = last_id
    if len(commands) > MAX_PER_POLL:
        log.warning("control: %d commands polled; processing the first %d",
                    len(commands), MAX_PER_POLL)
        commands = commands[:MAX_PER_POLL]
    if commands:
        log.info("control: polled %d command(s)", len(commands))
    return commands


def dispatch(commands: list[str], state: dict) -> dict:
    """Route each polled command string to its handler.

    Anything that doesn't match a known verb with well-formed args is logged
    and dropped. Handlers are idempotent, so duplicates and replays are
    harmless, and a handler failure never blocks the remaining commands.
    """
    for cmd in commands:
        try:
            m = _DONE_RE.match(cmd)
            if m:
                cmd_done(m.group(1), state)
                continue
            m = _SNOOZE_RE.match(cmd)
            if m:
                cmd_snooze(m.group(1), int(m.group(2)), state)
                continue
            m = _MUTE_RE.match(cmd)
            if m:
                cmd_mute(m.group(1), int(m.group(2)), state)
                continue
            log.warning("control: dropping unknown command %r", cmd[:80])
        except Exception as exc:  # noqa: BLE001 - one bad command never blocks the rest
            log.error("control: command %r failed: %s", cmd[:80], exc)
    return state


def cmd_done(habit_id: str, state: dict,
             now: Optional[_dt.datetime] = None) -> None:
    """Habit done: suppress its next scheduled nudge today (and only that one).

    Inserts the next slot key into the habit's existing dedup set, so
    habits._due_slots naturally skips it — habits.py needs no new read logic.
    "Next" is anchored on the clock (the first unsent slot strictly after
    now), not on the sent set, so a replayed/duplicated DONE re-targets the
    SAME slot (a set insert) instead of marching through the day. Unknown
    habit ids fail closed; no slots left after now is a no-op.
    """
    # Function-level import: habits imports control for its Done button, so a
    # module-level import here would be a cycle.
    from .topics import habits

    habit = next((h for h in habits._load() if h.get("name") == habit_id), None)
    if habit is None:
        log.warning("control: DONE for unknown habit %r dropped", habit_id)
        return
    now = now or _utcnow()
    # The next nudge is the first slot strictly after now (a slot already due
    # fires this same run anyway and is not what the user is dismissing). The
    # insert is an unconditional set add, so a duplicate DONE re-inserts the
    # same key instead of marching on to suppress further slots.
    upcoming = [h for h in habits._hours(habit) if h > now.hour]
    if not upcoming:
        log.info("control: DONE:%s - no slots left today; nothing to suppress",
                 habit_id)
        return
    skey = habits._state_key(habit_id)
    sent = set(state.get(skey) or [])
    sent.add(habits._slot_key(now.date(), upcoming[0]))
    state[skey] = sorted(sent)
    log.info("control: DONE:%s suppressed the %02d:00 UTC slot",
             habit_id, upcoming[0])


def cmd_snooze(reminder_id: str, minutes: int, state: dict,
               now: Optional[_dt.datetime] = None) -> None:
    """Snooze a reminder: re-deliver it ~minutes from now (clamped 5 min-30 d).

    Stores only id + until; topics/reminders.py recomputes the text from
    reminders.json at re-fire time (and drops snoozes whose id no longer
    exists), so an edited entry never re-fires stale. A repeated SNOOZE just
    overwrites the until — idempotent.
    """
    minutes = max(MIN_SNOOZE_MINUTES, min(MAX_SNOOZE_MINUTES, minutes))
    until = ((now or _utcnow()) + _dt.timedelta(minutes=minutes)).isoformat()
    state.setdefault(SNOOZED_KEY, {})[reminder_id] = until
    log.info("control: SNOOZE:%s until %s", reminder_id, until)


def cmd_mute(topic: str, hours: int, state: dict,
             now: Optional[_dt.datetime] = None) -> None:
    """Mute a topic for N hours (clamped 1 h-30 d).

    While the mute is active, events.emit downgrades the topic's routed
    actions: a live push is deferred into the morning digest (so nothing is
    lost, it just stops ringing) and a digest-bound item is dropped. Events
    with ``critical`` severity are exempt and still push, so muting a chatty
    news topic never silences a real alert. A repeated MUTE overwrites the
    until — idempotent.
    """
    hours = max(MIN_MUTE_HOURS, min(MAX_MUTE_HOURS, hours))
    until = ((now or _utcnow()) + _dt.timedelta(hours=hours)).isoformat()
    state.setdefault(MUTED_KEY, {})[topic] = until
    log.info("control: MUTE:%s until %s", topic, until)
