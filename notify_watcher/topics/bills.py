"""Topic: monthly utility-bill reminders (reminders.json -> bills, no network).

The reminders topic handles one-off and yearly dates; utility bills recur
MONTHLY on a day-of-month, which is a different shape — so they get their own
tiny engine over ``reminders.json`` -> ``bills``. Each entry names a bill
(EDEESTE electricity, CAASD water, internet/cable, ...) and the day of the
month it is due; we compute the next due date (clamping day 31 to a short
month's last day) and push once at each configured lead time before it
(default 5 and 1 days). Pure date math, so it never fails on a network hiccup;
daily-only (NOTIFY_DAILY) like reminders, and every (bill, occurrence, lead)
sent is remembered so nothing repeats. Editing a due day is a reminders.json
edit on github.com, not a code change.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from datetime import date
from pathlib import Path

from .. import events

log = logging.getLogger(__name__)

STATE_KEY = "bills_sent"
REMINDERS_PATH = Path(__file__).resolve().parent.parent.parent / "reminders.json"
_DEFAULT_LEADS = [5, 1]


def _load() -> list[dict]:
    try:
        data = json.loads(REMINDERS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.info("reminders.json not found; no bills to check")
        return []
    except json.JSONDecodeError as exc:
        log.error("reminders.json is not valid JSON: %s", exc)
        return []
    bills = data.get("bills") if isinstance(data, dict) else None
    return bills if isinstance(bills, list) else []


def _next_due(due_day: int, today: date) -> date:
    """Next date with day-of-month `due_day` on or after today, clamping to the
    month's last day (due_day 31 in June -> June 30, 30 in February -> Feb 28/29)."""
    y, m = today.year, today.month
    for _ in range(2):  # this month, else next month
        last = (date(y + (m == 12), m % 12 + 1, 1) - _dt.timedelta(days=1)).day
        cand = date(y, m, min(due_day, last))
        if cand >= today:
            return cand
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return cand  # unreachable: next month's clamped day is always >= today


def _due(bills: list[dict], today: date) -> list[tuple]:
    """Pure: bills firing today. Returns [(key, name, occurrence, days_left, note)]."""
    out: list[tuple] = []
    for b in bills:
        name = b.get("name")
        try:
            due_day = int(b.get("due_day"))
        except (ValueError, TypeError):
            due_day = 0
        if not name or not 1 <= due_day <= 31:
            continue
        occ = _next_due(due_day, today)
        days_left = (occ - today).days
        leads = b.get("lead_days") or _DEFAULT_LEADS
        for lead in leads:
            try:
                lead = int(lead)
            except (ValueError, TypeError):
                continue
            if days_left == lead:
                key = f"{b.get('id') or name}|{occ.isoformat()}|{lead}"
                out.append((key, name, occ, days_left, b.get("note", "")))
    return out


def run(state: dict) -> dict:
    if not os.environ.get("NOTIFY_DAILY"):
        return state  # daily-only, like reminders

    bills = _load()
    if not bills:
        return state

    sent_list = list(state.get(STATE_KEY) or [])
    sent_set = set(sent_list)
    new = 0

    for key, name, occ, days_left, note in _due(bills, _dt.date.today()):
        if key in sent_set:
            continue
        when = "tomorrow" if days_left == 1 else f"in {days_left} days"
        body = f"{name} - due {occ.isoformat()} ({when})"
        if note:
            body = f"{body}\n{note}"
        state = events.emit(
            state,
            title="Bill due soon",
            body=body,
            topic="bills",
            severity="high" if days_left <= 1 else "moderate",
            source="Bills",
            tags="money_with_wings",
            legacy_priority="high" if days_left <= 1 else "default",
            legacy_action="push",
        )
        sent_set.add(key)
        sent_list.append(key)
        new += 1

    if new:
        log.info("bills: sent %d", new)

    # Cap like reminders: keys embed the occurrence date so old ones never recur.
    state[STATE_KEY] = sent_list[-500:]
    return state
