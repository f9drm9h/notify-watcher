"""Topic: Dominican Republic public-holiday heads-up (Nager.Date, free, no key).

Nager.Date returns a country's public holidays as JSON. Each daily run we check
whether any holiday falls on one of the configured lead days (default: tomorrow
and today) and send a heads-up so an office/bank closure never catches you out.
Daily-only, and each (holiday, lead) fires once. We fetch this year and next so
a turn-of-year holiday (Jan 1) is still caught from a late-December run.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
from datetime import date

import requests

from .. import config, events

log = logging.getLogger(__name__)

STATE_KEY = "holidays_sent"
API = "https://date.nager.at/api/v3/PublicHolidays/{year}/{country}"
HEADERS = {"User-Agent": "notify-watcher/1.0 (+https://github.com/) personal-use"}
CAP = 60


def _due(holidays: list[dict], today: date, lead_days: list[int]) -> list[tuple]:
    """Pure: holidays landing on a lead day today. Returns [(key, name, date, days_until)]."""
    leads = {int(d) for d in lead_days}
    out: list[tuple] = []
    for h in holidays:
        try:
            hd = date.fromisoformat(h["date"])
        except (ValueError, TypeError, KeyError):
            continue
        days_until = (hd - today).days
        if days_until in leads:
            name = h.get("localName") or h.get("name") or "Holiday"
            out.append((f"{hd.isoformat()}|{days_until}", name, hd, days_until))
    return out


def _fetch(year: int, country: str) -> list[dict]:
    resp = requests.get(API.format(year=year, country=country), headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def run(state: dict) -> dict:
    if not os.environ.get("NOTIFY_DAILY"):
        return state

    cfg = config.section("holidays")
    country = cfg.get("country", "DO")
    lead_days = cfg.get("lead_days") or [1, 0]
    today = _dt.date.today()
    try:
        holidays = _fetch(today.year, country) + _fetch(today.year + 1, country)
    except Exception as exc:  # noqa: BLE001 - a fetch failure is non-fatal
        log.error("holidays fetch failed: %s", exc)
        return state

    sent_list = list(state.get(STATE_KEY) or [])
    sent_set = set(sent_list)
    new = 0
    for key, name, hd, days_until in _due(holidays, today, lead_days):
        if key in sent_set:
            continue
        when = "today" if days_until == 0 else f"in {days_until} day{'s' if days_until != 1 else ''}"
        events.emit(
            state,
            title="Public holiday",
            body=f"{name} - {hd.isoformat()} ({when}). Banks/offices likely closed.",
            topic="holidays",
            severity="moderate",
            source="Holidays",
            tags="date",
            legacy_priority="default",
            legacy_action="push",
        )
        sent_set.add(key)
        sent_list.append(key)
        new += 1

    if new:
        log.info("holidays: sent %d heads-up(s)", new)
    state[STATE_KEY] = sent_list[-CAP:]
    return state
