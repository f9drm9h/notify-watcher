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
  offers        : {offer_id: {kind,label,payload,created,applied}}  ADD targets
  ignored       : {offer_id: {label,since,was_applied}}  "Not interested" marks
  tracked_products : list[{name,url}]   ADDed products, merged by deals.run
  follows          : {artists/streamers/channels: list[dict]}  ADDed follows,
                     merged by music/twitch/youtube at read time
  watchlist_extra  : {movies/games: list[{name}]}  ADDed titles, merged by
                     watchlist.titles(category, state)
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
from typing import Optional

import requests

from . import ids, ntfy

log = logging.getLogger(__name__)

DEFAULT_SERVER = "https://ntfy.sh"
STATE_KEY = "control"
SNOOZED_KEY = "snoozed"
MUTED_KEY = "muted"
FOLLOWED_KEY = "followed"  # {topic: until_iso} — events.emit boosts digest->push
READING_LIST_KEY = "reading_list"
LATER_KEY = "later"
MORE_KEY = "more_requests"
OFFERS_KEY = "offers"
IGNORED_KEY = "ignored"
TRACKED_PRODUCTS_KEY = "tracked_products"
FOLLOWS_KEY = "follows"                  # {"artists": [], "streamers": [], "channels": []}
WATCHLIST_EXTRA_KEY = "watchlist_extra"  # {"movies": [], "games": []}

# Clamps bound every command's effect; a malformed or hostile duration can
# never snooze/mute longer than 30 days. At most MAX_PER_POLL commands are
# processed per run so a flooded control topic can't stall the watch run.
MIN_SNOOZE_MINUTES, MAX_SNOOZE_MINUTES = 5, 43_200  # 5 min .. 30 d
MIN_MUTE_HOURS, MAX_MUTE_HOURS = 1, 720             # 1 h .. 30 d
MIN_FOLLOW_HOURS, MAX_FOLLOW_HOURS = 1, 720         # 1 h .. 30 d
MIN_LATER_MINUTES, MAX_LATER_MINUTES = 5, 43_200    # 5 min .. 30 d
MAX_PER_POLL = 50
# Every phone-fillable list is capped so state.json growth is bounded by
# constants, not usage (docs/design/05 — security: bounded blast radius).
MAX_READING_LIST = 100
MAX_LATER = 20
MAX_OFFERS = 60
OFFER_TTL_DAYS = 14
MAX_IGNORED = 200
MAX_TRACKED_PRODUCTS = 25
MAX_FOLLOWS = 50          # per follows list (artists/streamers/channels)
MAX_WATCHLIST_EXTRA = 25  # per watchlist_extra list (movies/games)

# Per-kind overlay caps, enforced by _apply_offer.
_KIND_CAPS = {
    "product": MAX_TRACKED_PRODUCTS,
    "artist": MAX_FOLLOWS, "streamer": MAX_FOLLOWS, "channel": MAX_FOLLOWS,
    "movie": MAX_WATCHLIST_EXTRA, "game": MAX_WATCHLIST_EXTRA,
}
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
_FOLLOW_RE = re.compile(r"^FOLLOW:([A-Za-z0-9_-]+):(\d{1,4})$")
_UNFOLLOW_RE = re.compile(r"^UNFOLLOW:([A-Za-z0-9_-]+)$")
_READ_RE = re.compile(r"^READ:([0-9a-f]{16})$")
_MORE_RE = re.compile(r"^MORE:([0-9a-f]{16})$")
_LATER_RE = re.compile(r"^LATER:([0-9a-f]{16}):(\d{1,6})$")
_ADD_RE = re.compile(r"^ADD:([0-9a-f]{16})$")
_UNDO_RE = re.compile(r"^UNDO:([0-9a-f]{16})$")
_IGNORE_RE = re.compile(r"^IGNORE:([0-9a-f]{16})$")


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
            m = _FOLLOW_RE.match(cmd)
            if m:
                cmd_follow(m.group(1), int(m.group(2)), state)
                continue
            m = _UNFOLLOW_RE.match(cmd)
            if m:
                cmd_unfollow(m.group(1), state)
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
            m = _ADD_RE.match(cmd)
            if m:
                cmd_add(m.group(1), state)
                continue
            m = _UNDO_RE.match(cmd)
            if m:
                cmd_undo(m.group(1), state)
                continue
            m = _IGNORE_RE.match(cmd)
            if m:
                cmd_ignore(m.group(1), state)
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


def cmd_follow(topic: str, hours: int, state: dict,
               now: Optional[_dt.datetime] = None) -> None:
    """Follow a topic for N hours (clamped 1 h-30 d) — the mirror of MUTE.

    While active, events.emit upgrades the topic's digest-bound items to live
    pushes at default priority (drops stay dropped — a follow amplifies the
    middle band, it doesn't resurrect what the engine judged noise; and an
    active MUTE beats a follow). A repeated FOLLOW overwrites — idempotent.
    """
    hours = max(MIN_FOLLOW_HOURS, min(MAX_FOLLOW_HOURS, hours))
    until = ((now or _utcnow()) + _dt.timedelta(hours=hours)).isoformat()
    state.setdefault(FOLLOWED_KEY, {})[topic] = until
    log.info("control: FOLLOW:%s until %s", topic, until)


def cmd_unfollow(topic: str, state: dict) -> None:
    """End a topic's follow now. Unknown/unfollowed topic is a logged no-op."""
    if (state.get(FOLLOWED_KEY) or {}).pop(topic, None):
        log.info("control: UNFOLLOW:%s", topic)
    else:
        log.info("control: UNFOLLOW:%s - topic was not followed; nothing to do",
                 topic)


# --- Offer registry (ADD / UNDO / IGNORE) -----------------------------------
# The keystone of "notification-as-UI" (docs/design/05): a discovery push's
# button carries only an offer ID; the full payload (name, URL) was written to
# state by trusted topic code at push time. So a command can never inject a
# URL or name — it can only point at data we already chose to offer.

def offer_id(kind: str, payload: dict) -> str:
    """Content-derived offer id: the same discovery always maps to the same id.

    Keyed on the payload's most identifying field (url, then channel_id, then
    name), so a product re-discovered next month resolves to the same id —
    which is what makes register_offer idempotent and an IGNORE durable.
    """
    return ids.short(f"{kind}|{_payload_key(payload)}")


def register_offer(state: dict, kind: str, label: str, payload: dict,
                   applied: bool = False,
                   now: Optional[_dt.datetime] = None) -> Optional[str]:
    """Record an actionable discovery; returns its offer id, or None if the
    user already said "Not interested" (callers must then skip the offer).

    ``applied=True`` marks offers whose effect the topic applies itself at
    discovery time (e.g. soundcore_pro auto-tracks), so the button is the
    opt-OUT (IGNORE) and UNDO knows there is something to remove.
    Re-registering an existing offer refreshes label/payload and keeps
    created/applied — idempotent.
    """
    oid = offer_id(kind, payload)
    if oid in (state.get(IGNORED_KEY) or {}):
        log.info("control: offer %s (%r) is ignored; not offering", oid, label)
        return None
    offers = state.setdefault(OFFERS_KEY, {})
    existing = offers.get(oid)
    now_iso = (now or _utcnow()).isoformat()
    offers[oid] = {
        "kind": kind,
        "label": label,
        "payload": dict(payload),
        "created": existing.get("created", now_iso) if existing else now_iso,
        "applied": (existing or {}).get("applied") or (now_iso if applied else None),
    }
    return oid


def _overlay_lists(state: dict, kind: str) -> list[list]:
    """The state overlay list(s) an offer kind's payload lives in.

    For products both the ADD overlay and the auto-track list are returned, so
    UNDO/IGNORE of an auto-tracked discovery removes it wherever it landed.
    Topics merge these overlays with their config at read time (deals, music,
    twitch, youtube, watchlist.titles) — config files are never written.
    """
    if kind == "product":
        return [state.setdefault(TRACKED_PRODUCTS_KEY, []),
                state.setdefault("auto_products", [])]
    if kind in ("artist", "streamer", "channel"):
        plural = {"artist": "artists", "streamer": "streamers",
                  "channel": "channels"}[kind]
        return [state.setdefault(FOLLOWS_KEY, {}).setdefault(plural, [])]
    if kind in ("movie", "game"):
        return [state.setdefault(WATCHLIST_EXTRA_KEY, {})
                     .setdefault(kind + "s", [])]
    return []


def _payload_key(payload: dict) -> str:
    return str(payload.get("url") or payload.get("channel_id")
               or payload.get("name") or "")


def follows(state: dict, plural: str) -> list[dict]:
    """The follow overlay's dict entries for "artists"/"streamers"/"channels".

    Read-time merge helper for music/twitch/youtube; tolerates missing keys
    and non-dict junk so a hand-edited state.json can't crash a topic.
    """
    raw = (state.get(FOLLOWS_KEY) or {}).get(plural) or []
    return [e for e in raw if isinstance(e, dict)]


def extra_titles(state: dict, category: str) -> list[str]:
    """The watchlist_extra overlay's names for "movies"/"games"."""
    raw = (state.get(WATCHLIST_EXTRA_KEY) or {}).get(category) or []
    return [str(e["name"]) for e in raw
            if isinstance(e, dict) and e.get("name")]


def _apply_offer(state: dict, offer: dict) -> bool:
    """Add the offer's payload to its overlay (idempotent). False = unknown kind/full."""
    kind, payload = offer.get("kind", ""), offer.get("payload") or {}
    lists = _overlay_lists(state, kind)
    if not lists:
        log.warning("control: cannot apply offer of unknown kind %r", kind)
        return False
    target = lists[0]
    key = _payload_key(payload)
    for lst in lists:
        if any(isinstance(p, dict) and _payload_key(p) == key for p in lst):
            return True  # already applied somewhere — idempotent
    cap = _KIND_CAPS.get(kind)
    if cap and len(target) >= cap:
        log.warning("control: %s overlay full (%d); not adding %r",
                    kind, cap, offer.get("label"))
        return False
    target.append(dict(payload))
    return True


def _remove_offer_payload(state: dict, offer: dict) -> None:
    """Remove the offer's payload from every overlay it may live in."""
    key = _payload_key(offer.get("payload") or {})
    for lst in _overlay_lists(state, offer.get("kind", "")):
        lst[:] = [p for p in lst
                  if not (isinstance(p, dict) and _payload_key(p) == key)]


def cmd_add(oid: str, state: dict, now: Optional[_dt.datetime] = None) -> None:
    """Apply a registered offer (e.g. start price-tracking). Unknown id drops."""
    offer = (state.get(OFFERS_KEY) or {}).get(oid)
    if offer is None:
        log.warning("control: ADD for unknown offer %s dropped", oid)
        return
    (state.get(IGNORED_KEY) or {}).pop(oid, None)  # an ADD overrides a prior ignore
    if _apply_offer(state, offer):
        offer["applied"] = (now or _utcnow()).isoformat()
        log.info("control: ADD %s applied (%s %r)", oid, offer.get("kind"),
                 offer.get("label"))


def cmd_ignore(oid: str, state: dict,
               now: Optional[_dt.datetime] = None) -> None:
    """Not interested: un-apply the offer (if applied) and never offer it again.

    Reversible via UNDO (which also restores an auto-applied effect) or a
    state.json edit. The ignored map is FIFO-capped at MAX_IGNORED.
    """
    offer = (state.get(OFFERS_KEY) or {}).get(oid)
    if offer is None:
        log.warning("control: IGNORE for unknown offer %s dropped", oid)
        return
    was_applied = bool(offer.get("applied"))
    if was_applied:
        _remove_offer_payload(state, offer)
        offer["applied"] = None
    ignored = state.setdefault(IGNORED_KEY, {})
    ignored[oid] = {
        "label": offer.get("label", ""),
        "since": (now or _utcnow()).isoformat(),
        "was_applied": was_applied,
    }
    while len(ignored) > MAX_IGNORED:
        ignored.pop(next(iter(ignored)))
    log.info("control: IGNORE %s (%r)", oid, offer.get("label"))


def cmd_undo(oid: str, state: dict, now: Optional[_dt.datetime] = None) -> None:
    """Reverse exactly what ADD or IGNORE of this offer did. No-ops are logged.

    Undoing an IGNORE also re-applies the offer when the ignore had un-applied
    it (the auto-track "Not interested" case), restoring the pre-tap state.
    """
    offer = (state.get(OFFERS_KEY) or {}).get(oid)
    if offer is None:
        log.warning("control: UNDO for unknown offer %s dropped", oid)
        return
    mark = (state.get(IGNORED_KEY) or {}).pop(oid, None)
    if mark is not None:
        if mark.get("was_applied") and _apply_offer(state, offer):
            offer["applied"] = (now or _utcnow()).isoformat()
        log.info("control: UNDO %s un-ignored (%r)", oid, offer.get("label"))
        return
    if offer.get("applied"):
        _remove_offer_payload(state, offer)
        offer["applied"] = None
        log.info("control: UNDO %s un-applied (%r)", oid, offer.get("label"))
        return
    log.info("control: UNDO %s - nothing to reverse", oid)


def _prune_offers(state: dict, now: _dt.datetime) -> None:
    """Expire unapplied offers past their TTL and hard-cap the registry.

    Applied offers are kept while possible (they are the UNDO record); when
    the cap forces eviction, oldest-created go first regardless. A pruned
    offer just means its button goes dead — the tap is logged and dropped.
    """
    offers = state.get(OFFERS_KEY)
    if not offers:
        return
    cutoff = (now - _dt.timedelta(days=OFFER_TTL_DAYS)).isoformat()
    for oid, offer in list(offers.items()):
        if not isinstance(offer, dict):
            offers.pop(oid, None)
        elif not offer.get("applied") and str(offer.get("created", "")) < cutoff:
            offers.pop(oid, None)
    if len(offers) > MAX_OFFERS:
        by_age = sorted(offers,
                        key=lambda o: (bool(offers[o].get("applied")),
                                       str(offers[o].get("created", ""))))
        for oid in by_age[: len(offers) - MAX_OFFERS]:
            offers.pop(oid, None)


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
    retry next run; one bad entry never blocks the rest. Also prunes the
    offer registry (TTL + cap) so it stays bounded.
    """
    now = now or _utcnow()
    _prune_offers(state, now)

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
