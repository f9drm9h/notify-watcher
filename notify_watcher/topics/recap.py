"""Topic: weekly recap — one Monday-morning summary of the past week's activity.

The event log (``state["event_log"]``) records every routed Event and
``state["topic_health"]`` records every topic's last outcome, but that history
is only visible on the locally-rendered dashboard. This topic turns it into one
calm push on the first daily run of each ISO week (Monday ~08:00 DR):

    Your week in notifications
    14 live pushes, 31 digested, 52 dropped
    Busiest: movies (61), fda (9), twitch (4)
    Top story: [82] Hurricane watch issued for the Dominican Republic
    All 30 topics healthy

Pure state inspection — no network, no key, nothing to configure. Deduped per
ISO week (``recap_last_week``), so a duplicate or drifted daily run never
double-sends; a failed send naturally retries on the next daily run. With an
empty event log (fresh install) it stays silent.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
from collections import Counter

from .. import events
from ..eventlog import EVENT_LOG_KEY

log = logging.getLogger(__name__)

STATE_KEY = "recap_last_week"
WINDOW_DAYS = 7
TOP_TOPICS = 3


def _iso_week(day: _dt.date) -> str:
    y, w, _ = day.isocalendar()
    return f"{y}-W{w:02d}"


def _parse_ts(ts) -> _dt.datetime | None:
    if not ts:
        return None
    try:
        dt = _dt.datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt


def _window(event_log: list, now: _dt.datetime, days: int = WINDOW_DAYS) -> list[dict]:
    """The log entries from the trailing window; unparseable timestamps are skipped."""
    cutoff = now - _dt.timedelta(days=days)
    out = []
    for entry in event_log:
        if not isinstance(entry, dict):
            continue
        ts = _parse_ts(entry.get("ts"))
        if ts is not None and ts >= cutoff:
            out.append(entry)
    return out


def _summarize(entries: list[dict], health: dict,
               reading_list_count: int = 0) -> str:
    """Pure. Render the recap body from one week of log entries + topic health."""
    actions = Counter(e.get("action") for e in entries)
    lines = [
        f"{actions.get('push', 0)} live pushes, {actions.get('digest', 0)} digested, "
        f"{actions.get('drop', 0)} dropped"
    ]
    if reading_list_count:
        # Items saved via the [Read later] reply button (docs/design/05);
        # the full list with links is on the dashboard.
        lines.append(f"Reading list: {reading_list_count} saved item(s)")

    by_topic = Counter(e.get("topic") for e in entries if e.get("topic"))
    if by_topic:
        busiest = ", ".join(f"{t} ({n})" for t, n in by_topic.most_common(TOP_TOPICS))
        lines.append(f"Busiest: {busiest}")

    pushed = [e for e in entries if e.get("action") == "push" and e.get("title")]
    if pushed:
        top = max(pushed, key=lambda e: int(e.get("score") or 0))
        lines.append(f"Top story: [{int(top.get('score') or 0)}] {top['title']}")

    failing = sorted(
        name for name, entry in health.items()
        if isinstance(entry, dict) and entry.get("last_error")
    )
    if failing:
        lines.append(f"Failing now: {', '.join(failing)}")
    elif health:
        lines.append(f"All {len(health)} topics healthy")
    return "\n".join(lines)


def run(state: dict) -> dict:
    if not os.environ.get("NOTIFY_DAILY"):
        return state  # weekly work rides the daily run, like games/fx
    now = _dt.datetime.now(_dt.timezone.utc)
    week = _iso_week(now.date())
    if state.get(STATE_KEY) == week:
        return state

    entries = _window(state.get(EVENT_LOG_KEY) or [], now)
    if not entries:
        log.info("recap: no event-log history this week; skipping")
        state[STATE_KEY] = week
        return state

    body = _summarize(entries, state.get("topic_health") or {},
                      len(state.get("reading_list") or []))
    state = events.emit(
        state,
        title="Your week in notifications",
        body=body,
        topic="recap",
        severity="low",
        source="Recap",
        tags="bar_chart",
        legacy_priority="low",
        legacy_action="push",
    )
    log.info("recap: sent weekly summary for %s (%d events)", week, len(entries))
    state[STATE_KEY] = week
    return state
