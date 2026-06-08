"""Topic: personal expiry & deadline reminders (reminders.json, no network).

A tiny date engine over reminders.json: passport/visa/ID expiry, subscription
renewals, warranties, birthdays. For each entry it computes the upcoming
occurrence (one-off, or the next yearly recurrence) and fires once at each
configured lead time before it (default 90/30/7/1/0 days), so a deadline gives a
gentle heads-up tapering to a day-of nudge. Pure date math, so it never fails on
a network hiccup; it runs on the daily cron only (NOTIFY_DAILY) and remembers
every (reminder, occurrence, lead) it has sent so nothing repeats.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from datetime import date
from pathlib import Path

from .. import ntfy

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


def run(state: dict) -> dict:
    if not os.environ.get("NOTIFY_DAILY"):
        return state  # daily-only, like health_tip / digest / learn

    reminders = _load()
    if not reminders:
        return state

    due = _due(reminders, _dt.date.today())
    sent_list = list(state.get(STATE_KEY) or [])
    sent_set = set(sent_list)
    new = 0

    for key, name, occ, days_left, note in due:
        if key in sent_set:
            continue
        when = "today" if days_left == 0 else f"in {days_left} day{'s' if days_left != 1 else ''}"
        body = f"{name} - {occ.isoformat()} ({when})"
        if note:
            body = f"{body}\n{note}"
        ntfy.push(
            title="Reminder",
            message=body,
            tags="calendar",
            priority="high" if days_left <= 7 else "default",
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
