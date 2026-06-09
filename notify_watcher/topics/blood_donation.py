"""Topic: blood-donation eligibility reminder (no network, daily-only).

Whole-blood donors can give again after a fixed interval (56 days in most
programs). We compute the eligible date from monitors.json -> blood_donation
(last_donation + interval_days) and, once it has passed, send a gentle nudge -
then re-nudge at most every renotify_days while you remain eligible and haven't
logged a new donation. Update last_donation each time you donate to reset the
cycle. Pure date math, so it never fails on a network hiccup.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
from datetime import date, timedelta

from .. import config, events

log = logging.getLogger(__name__)

STATE_KEY = "blood_donation_last_notified"


def _should_notify(last_donation: date, interval_days: int, renotify_days: int,
                   last_notified, today: date) -> tuple[bool, date]:
    """Return (notify?, eligible_date). Notify once eligible, then at most every
    renotify_days until last_donation is updated."""
    eligible = last_donation + timedelta(days=int(interval_days))
    if today < eligible:
        return False, eligible
    if last_notified and (today - last_notified).days < int(renotify_days):
        return False, eligible
    return True, eligible


def run(state: dict) -> dict:
    if not os.environ.get("NOTIFY_DAILY"):
        return state  # daily-only, like reminders / health_tip / learn

    cfg = config.section("blood_donation")
    raw = cfg.get("last_donation")
    if not raw:
        return state
    try:
        last_donation = date.fromisoformat(raw)
    except (ValueError, TypeError):
        log.error("blood_donation.last_donation is not a valid date: %r", raw)
        return state

    today = _dt.date.today()
    last_notified_raw = state.get(STATE_KEY)
    last_notified = None
    if last_notified_raw:
        try:
            last_notified = date.fromisoformat(last_notified_raw)
        except (ValueError, TypeError):
            last_notified = None

    notify, eligible = _should_notify(
        last_donation,
        cfg.get("interval_days", 56),
        cfg.get("renotify_days", 30),
        last_notified,
        today,
    )
    if not notify:
        return state

    events.emit(
        state,
        title="Blood donation",
        body=(f"You're eligible to donate blood again "
              f"(last donation {last_donation.isoformat()}, eligible since "
              f"{eligible.isoformat()})."),
        topic="blood_donation",
        severity="moderate",
        source="Blood donation",
        tags="drop_of_blood",
        legacy_priority="default",
        legacy_action="push",
    )
    log.info("blood donation: sent eligibility reminder")
    state[STATE_KEY] = today.isoformat()
    return state
