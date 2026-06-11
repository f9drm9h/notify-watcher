"""Topic: personal expiry & deadline reminders (reminders.json, no network).

A tiny date engine over reminders.json: passport/visa/ID expiry, subscription
renewals, warranties, birthdays. For each entry it computes the upcoming
occurrence (one-off, or the next yearly recurrence) and fires once at each
configured lead time before it (default 90/30/7/1/0 days), so a deadline gives a
gentle heads-up tapering to a day-of nudge. Pure date math, so it never fails on
a network hiccup; it runs on the daily cron only (NOTIFY_DAILY) and remembers
every (reminder, occurrence, lead) it has sent so nothing repeats.

Reply buttons: entries with an ``id`` slug get a [Snooze 1h] button that POSTs
``SNOOZE:{id}:60`` to the control topic (entries without an id simply get no
button). control.cmd_snooze records ``state["snoozed"][id] = until_iso``; the
re-fire check here runs every cycle (before the NOTIFY_DAILY gate) and
re-delivers a lapsed snooze, recomputing the text from reminders.json so an
edited entry never re-fires stale. Effective snooze latency is bounded by the
3-hourly full run. A snooze is an EXTRA delivery: it never touches
``reminders_sent``, so the normal lead-day ladder is unaffected.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from datetime import date
from pathlib import Path

from .. import control, events

log = logging.getLogger(__name__)

STATE_KEY = "reminders_sent"
REMINDERS_PATH = Path(__file__).resolve().parent.parent.parent / "reminders.json"
_DEFAULT_LEADS = [90, 30, 7, 1, 0]


def _load() -> list[dict]:
    try:
        data = json.loads(REMINDERS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.info("reminders.json not found; nothing to do")
        return []
    except json.JSONDecodeError as exc:
        log.error("reminders.json is not valid JSON: %s", exc)
        return []
    rems = data.get("reminders") if isinstance(data, dict) else None
    return rems if isinstance(rems, list) else []


def _next_occurrence(base: date, today: date, recurring: str) -> date | None:
    """Upcoming occurrence of `base`. One-off returns base; yearly returns the
    next base month/day on or after today (Feb 29 -> Feb 28 in non-leap years)."""
    if recurring != "yearly":
        return base
    for y in (today.year, today.year + 1):
        try:
            cand = base.replace(year=y)
        except ValueError:  # Feb 29 in a non-leap year
            cand = date(y, 2, 28) if (base.month == 2 and base.day == 29) else None
        if cand and cand >= today:
            return cand
    return None


def _due(reminders: list[dict], today: date) -> list[tuple]:
    """Pure: reminders firing today. Returns [(key, name, occurrence, days_left, note)]."""
    out: list[tuple] = []
    for r in reminders:
        name = r.get("name")
        date_str = r.get("date")
        if not name or not date_str:
            continue
        try:
            base = date.fromisoformat(date_str)
        except (ValueError, TypeError):
            continue
        occ = _next_occurrence(base, today, (r.get("recurring") or "").lower())
        if occ is None:
            continue
        days_left = (occ - today).days
        leads = r.get("lead_days") or _DEFAULT_LEADS
        for lead in leads:
            try:
                lead = int(lead)
            except (ValueError, TypeError):
                continue
            if days_left == lead:
                key = f"{name}|{occ.isoformat()}|{lead}"
                out.append((key, name, occ, days_left, r.get("note", "")))
    return out


def _snooze_action(rid: object):
    """The [Snooze 1h] button for a reminder id, or None (no id / control off)."""
    if not rid or not isinstance(rid, str):
        return None
    return control.make_action("Snooze 1h", f"SNOOZE:{rid}:60")


def _refire_snoozed(state: dict, reminders: list[dict], today: date) -> None:
    """Re-deliver snoozed reminders whose snooze has lapsed.

    Runs every cycle (before the NOTIFY_DAILY gate), so a snooze re-fires on
    the next full run after its `until` passes. The entry's text is recomputed
    from reminders.json; a snooze whose id no longer exists (or whose entry is
    now malformed/past) is dropped with a log line. Each snooze is consumed on
    processing, so a re-fire happens at most once.
    """
    snoozes = state.get(control.SNOOZED_KEY)
    if not isinstance(snoozes, dict) or not snoozes:
        return
    for rid in list(snoozes):
        if control.until_active(snoozes[rid]):
            continue  # still snoozed
        until = snoozes.pop(rid)
        entry = next((r for r in reminders if r.get("id") == rid), None)
        if entry is None:
            log.info("snooze for unknown reminder id %r dropped", rid)
            continue
        try:
            base = date.fromisoformat(entry.get("date") or "")
        except (ValueError, TypeError):
            log.info("snoozed reminder %r has no valid date; dropped", rid)
            continue
        occ = _next_occurrence(base, today, (entry.get("recurring") or "").lower())
        if occ is None:
            continue
        days_left = (occ - today).days
        when = "today" if days_left == 0 else f"in {days_left} day{'s' if days_left != 1 else ''}"
        body = f"{entry.get('name', rid)} - {occ.isoformat()} ({when})"
        if entry.get("note"):
            body = f"{body}\n{entry['note']}"
        snooze = _snooze_action(rid)
        events.emit(
            state,
            title="Reminder (snoozed)",
            body=body,
            topic="reminders",
            severity="high" if days_left <= 7 else "moderate",
            source="Reminders",
            tags="calendar",
            legacy_priority="high" if days_left <= 7 else "default",
            legacy_action="push",
            metadata={"actions": [snooze]} if snooze else None,
        )
        log.info("re-fired snoozed reminder %r (was until %s)", rid, until)


def run(state: dict) -> dict:
    reminders = _load()

    # Snooze re-fires run every cycle so a lapsed snooze isn't held for the
    # daily gate; the regular lead-day ladder below stays daily-only.
    _refire_snoozed(state, reminders, _dt.date.today())

    if not os.environ.get("NOTIFY_DAILY"):
        return state  # daily-only, like health_tip / digest / learn

    if not reminders:
        return state

    ids = {r.get("name"): r.get("id") for r in reminders if isinstance(r, dict)}
    snoozes = state.get(control.SNOOZED_KEY) or {}
    due = _due(reminders, _dt.date.today())
    sent_list = list(state.get(STATE_KEY) or [])
    sent_set = set(sent_list)
    new = 0

    for key, name, occ, days_left, note in due:
        if key in sent_set:
            continue
        rid = ids.get(name)
        if rid and control.until_active(snoozes.get(rid)):
            # Actively snoozed: the user said "not now", so this lead-day rung
            # is skipped (the snooze re-fire is the redelivery). Marked sent so
            # the rung isn't replayed once the snooze lapses.
            log.info("reminder %r snoozed; skipping lead-day push %s", name, key)
            sent_set.add(key)
            sent_list.append(key)
            continue
        when = "today" if days_left == 0 else f"in {days_left} day{'s' if days_left != 1 else ''}"
        body = f"{name} - {occ.isoformat()} ({when})"
        if note:
            body = f"{body}\n{note}"
        snooze = _snooze_action(rid)
        events.emit(
            state,
            title="Reminder",
            body=body,
            topic="reminders",
            severity="high" if days_left <= 7 else "moderate",
            source="Reminders",
            tags="calendar",
            legacy_priority="high" if days_left <= 7 else "default",
            legacy_action="push",
            metadata={"actions": [snooze]} if snooze else None,
        )
        sent_set.add(key)
        sent_list.append(key)
        new += 1

    if new:
        log.info("reminders: sent %d", new)

    # Cap so the seen-list can't grow without bound; old keys never recur (the
    # occurrence date is part of the key), and yearly entries mint a new key each
    # year, so trimming the oldest is safe.
    state[STATE_KEY] = sent_list[-1000:]
    return state
