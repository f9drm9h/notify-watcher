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
  control       : {"last_id": str}   poll cursor (newest processed ntfy message id)
  snoozed       : {reminder_id: until_iso}   read by topics/reminders.py
  muted         : {topic: until_iso}         read by events.emit
  reading_list  : list[{id,title,url,source,added}]   READ saves (recap/dashboard)
  later         : {event_id: {until, snapshot}}       LATER re-fire queue
  more_requests : {event_id: true}                    MORE pushes pending
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
from typing import Optional

import requests

from . import ntfy

log = logging.getLogger(__name__)

DEFAULT_SERVER = "https://ntfy.sh"
STATE_KEY = "control"
SNOOZED_KEY = "snoozed"
MUTED_KEY = "muted"
READING_LIST_KEY = "reading_list"
LATER_KEY = "later"
MORE_KEY = "more_requests"

# Clamps bound every command's effect; a malformed or hostile duration can
# never snooze/mute longer than 30 days. At most MAX_PER_POLL commands are
# processed per run so a flooded control topic can't stall the watch run.
MIN_SNOOZE_MINUTES, MAX_SNOOZE_MINUTES = 5, 43_200  # 5 min .. 30 d
MIN_MUTE_HOURS, MAX_MUTE_HOURS = 1, 720             # 1 h .. 30 d
MIN_LATER_MINUTES, MAX_LATER_MINUTES = 5, 43_200    # 5 min .. 30 d
MAX_PER_POLL = 50
# Every phone-fillable list is capped so state.json growth is bounded by
# constants, not usage (docs/design/05 — security: bounded blast radius).
MAX_READING_LIST = 100
MAX_LATER = 20
# "Show more" related-items window: same topic, last N days, up to M lines.
RELATED_DAYS = 7
RELATED_MAX = 3

# Strict per-verb grammar: ASCII slug ids, numeric durations. Anything that
# doesn't match a verb exactly is logged and dropped (fail closed). Item-level
# verbs (READ/MORE/LATER) take an event-log id — 16 hex chars from
# eventlog.entry_id — and resolve it against state, so a command can never
# carry a URL or free text into the system (reference, not payload).
_DONE_RE = re.compile(r"^DONE:([A-Za-z0-9_-]+)$")
_SNOOZE_RE = re.compile(r"^SNOOZE:([A-Za-z0-9_-]+):(\d{1,6})$")
_MUTE_RE = re.compile(r"^MUTE:([A-Za-z0-9_-]+):(\d{1,4})$")
_UNMUTE_RE = re.compile(r"^UNMUTE:([A-Za-z0-9_-]+)$")
_READ_RE = re.compile(r"^READ:([0-9a-f]{16})$")
_MORE_RE = re.compile(r"^MORE:([0-9a-f]{16})$")
_LATER_RE = re.compile(r"^LATER:([0-9a-f]{16}):(\d{1,6})$")


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
            m = _UNMUTE_RE.match(cmd)
            if m:
                cmd_unmute(m.group(1), state)
                continue
            m = _READ_RE.match(cmd)
            if m:
                cmd_read(m.group(1), state)
                continue
            m = _MORE_RE.match(cmd)
            if m:
                cmd_more(m.group(1), state)
                continue
            m = _LATER_RE.match(cmd)
            if m:
                cmd_later(m.group(1), int(m.group(2)), state)
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


def cmd_unmute(topic: str, state: dict) -> None:
    """End a topic's mute now. Unknown/unmuted topic is a logged no-op."""
    if (state.get(MUTED_KEY) or {}).pop(topic, None):
        log.info("control: UNMUTE:%s", topic)
    else:
        log.info("control: UNMUTE:%s - topic was not muted; nothing to do", topic)


def _find_event(state: dict, event_id: str) -> Optional[dict]:
    """Resolve an event-log id to {title, detail, url, source, topic, ts}.

    Searches the event log newest-first, then falls back to a pending LATER
    snapshot (which holds the same fields), so a Remind-again tap on a
    re-fired push still resolves while the entry is queued. None when the id
    is unknown or has aged out of the 500-entry ring — callers fail closed.
    """
    for entry in reversed(state.get("event_log") or []):
        if isinstance(entry, dict) and entry.get("id") == event_id:
            return entry
    pending = (state.get(LATER_KEY) or {}).get(event_id)
    if isinstance(pending, dict) and isinstance(pending.get("snapshot"), dict):
        return pending["snapshot"]
    return None


def cmd_read(event_id: str, state: dict,
             now: Optional[_dt.datetime] = None) -> None:
    """Read later: save the pushed item to state["reading_list"].

    Stores only fields copied from the event log (reference, not payload).
    Idempotent: an id already on the list is a no-op. The list is FIFO-capped
    at MAX_READING_LIST so it can never grow without bound.
    """
    entry = _find_event(state, event_id)
    if entry is None:
        log.warning("control: READ for unknown event %s dropped", event_id)
        return
    items = state.setdefault(READING_LIST_KEY, [])
    if any(isinstance(it, dict) and it.get("id") == event_id for it in items):
        log.info("control: READ:%s already saved; nothing to do", event_id)
        return
    items.append({
        "id": event_id,
        "title": entry.get("title", ""),
        "url": entry.get("url", ""),
        "source": entry.get("source", ""),
        "added": (now or _utcnow()).isoformat(),
    })
    if len(items) > MAX_READING_LIST:
        del items[: len(items) - MAX_READING_LIST]
    log.info("control: READ:%s saved %r", event_id, entry.get("title", "")[:60])


def cmd_more(event_id: str, state: dict) -> None:
    """Show more: queue a fuller-story push, sent by process_pending this run.

    Idempotent — repeat taps before the next run collapse into one request.
    """
    if _find_event(state, event_id) is None:
        log.warning("control: MORE for unknown event %s dropped", event_id)
        return
    state.setdefault(MORE_KEY, {})[event_id] = True
    log.info("control: MORE:%s queued", event_id)


def cmd_later(event_id: str, minutes: int, state: dict,
              now: Optional[_dt.datetime] = None) -> None:
    """Remind later: re-push a snapshot of the event after ~minutes (clamped).

    The snapshot is copied out of the event log NOW, so the 500-entry ring
    aging out before the re-fire doesn't matter. A repeated LATER overwrites
    the until (idempotent); the pending queue is capped at MAX_LATER.
    """
    entry = _find_event(state, event_id)
    if entry is None:
        log.warning("control: LATER for unknown event %s dropped", event_id)
        return
    later = state.setdefault(LATER_KEY, {})
    if event_id not in later and len(later) >= MAX_LATER:
        log.warning("control: LATER queue full (%d); dropping %s",
                    MAX_LATER, event_id)
        return
    minutes = max(MIN_LATER_MINUTES, min(MAX_LATER_MINUTES, minutes))
    until = ((now or _utcnow()) + _dt.timedelta(minutes=minutes)).isoformat()
    later[event_id] = {
        "until": until,
        "snapshot": {
            "title": entry.get("title", ""),
            "detail": entry.get("detail", ""),
            "url": entry.get("url", ""),
            "source": entry.get("source", ""),
            "topic": entry.get("topic", ""),
        },
    }
    log.info("control: LATER:%s until %s", event_id, until)


def _related_lines(state: dict, event_id: str, topic: str,
                   now: _dt.datetime) -> list[str]:
    """Up to RELATED_MAX recent same-topic event-log titles (the cheap
    'fuller story' built from data we already hold — no scraping)."""
    cutoff = now - _dt.timedelta(days=RELATED_DAYS)
    lines: list[str] = []
    for entry in reversed(state.get("event_log") or []):
        if len(lines) >= RELATED_MAX:
            break
        if not isinstance(entry, dict) or entry.get("id") == event_id:
            continue
        if entry.get("topic") != topic or not entry.get("title"):
            continue
        try:
            ts = _dt.datetime.fromisoformat(str(entry.get("ts")))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=_dt.timezone.utc)
        except ValueError:
            continue
        if ts >= cutoff:
            lines.append(f"- {entry['title']}")
    return lines


def process_pending(state: dict, now: Optional[_dt.datetime] = None) -> None:
    """Send due LATER re-fires and queued MORE pushes. Called after dispatch.

    Runs every cycle (including the 15-min twitch runs), so LATER honors its
    requested time within ~15 min. These pushes bypass the priority engine
    deliberately: the user explicitly asked for each one, so it must not be
    digested or dropped by routing. Per-entry failures keep the entry for a
    retry next run; one bad entry never blocks the rest.
    """
    now = now or _utcnow()

    later = state.get(LATER_KEY) or {}
    for event_id, entry in list(later.items()):
        if not isinstance(entry, dict) or until_active(entry.get("until"), now):
            if not isinstance(entry, dict):
                later.pop(event_id, None)  # malformed: drop, don't wedge
            continue
        snap = entry.get("snapshot") or {}
        actions = [a for a in (
            make_action("Remind 3h", f"LATER:{event_id}:180"),
            make_action("Read later", f"READ:{event_id}"),
        ) if a]
        try:
            ntfy.push(
                title=f"Reminder: {snap.get('title', '')}",
                message=snap.get("detail") or snap.get("title") or "",
                click_url=snap.get("url") or None,
                tags="alarm_clock",
                priority="default",
                **({"actions": actions} if actions else {}),
            )
        except Exception as exc:  # noqa: BLE001 - keep for retry next run
            log.warning("control: LATER re-fire for %s failed (will retry): %s",
                        event_id, exc)
            continue
        later.pop(event_id, None)
        log.info("control: LATER re-fired %s", event_id)

    more = state.get(MORE_KEY) or {}
    for event_id in list(more):
        entry = _find_event(state, event_id)
        if entry is None:
            more.pop(event_id, None)
            log.warning("control: MORE target %s no longer in the log; dropped",
                        event_id)
            continue
        lines = [entry.get("detail") or "(no further detail held - tap to open)"]
        related = _related_lines(state, event_id, entry.get("topic", ""), now)
        if related:
            lines += ["", "Also on this topic recently:"] + related
        try:
            ntfy.push(
                title=f"More: {entry.get('title', '')}",
                message="\n".join(lines),
                click_url=entry.get("url") or None,
                tags="mag",
                priority="default",
            )
        except Exception as exc:  # noqa: BLE001 - keep for retry next run
            log.warning("control: MORE push for %s failed (will retry): %s",
                        event_id, exc)
            continue
        more.pop(event_id, None)
        log.info("control: MORE sent for %s", event_id)
