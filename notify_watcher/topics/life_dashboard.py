"""Topic: weekly life dashboard — one rich Sunday digest of the past week.

The event log (``state["event_log"]``), ``state["topic_health"]`` and a handful
of per-module state keys (fx rate, bills config, spending summary) each hold a
slice of the week's activity, but individually they only surface as separate
pushes or on the locally-rendered dashboard. This topic stitches them into one
calm Sunday push with themed, emoji-headed sections::

    💪 HABITS
    - Habit nudges sent: water (14), stretch (7)

    💰 FINANCE
    - Spending: spent RD$4,210 last week (-12%)
    - USD/DOP: 60.10 -> 60.80 (+0.70, +1.16%)
    - Upcoming bills: Rent due 2026-06-30 (in 5 days)

    🌤 WEATHER & ENVIRONMENT
    - Weather alerts logged: 2 (latest: Hurricane watch issued)
    - UV / air-quality warnings: 1 (latest: High UV index tomorrow)

    🎬 ENTERTAINMENT & NEWS
    - Top release: [78] Hollow Knight: Silksong — release date set
    - Golden Sun: 2 community update(s); latest: "..."
    - AI news: 1 item; latest: "..."

    ⚙️ SYSTEM HEALTH
    - Topics with errors this week: energy, fuel
    - 47 notifications pushed this week

Pure state inspection — no network, no key, nothing to configure. It rides the
daily run (like recap/games) but fires only on **Sunday**, deduped per ISO week
(``life_dashboard_last_week``) so a duplicate or drifted Sunday run never
double-sends. Empty sections are skipped gracefully; if the whole week has no
data it stays silent but still stamps the week. Priority: low.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
from collections import Counter

from .. import events
from ..eventlog import EVENT_LOG_KEY
from . import bills as bills_topic

log = logging.getLogger(__name__)

STATE_KEY = "life_dashboard_last_week"
WINDOW_DAYS = 7
SUNDAY = 6  # date.weekday(): Monday=0 ... Sunday=6

# Event-log topic groupings for the themed sections.
_WEATHER_TOPICS = ("weather", "onamet", "marine")
_ENV_TOPICS = ("uv", "air_quality")
_RELEASE_TOPICS = ("movies", "games")


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


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
    """The log entries from the trailing window; unparseable entries are skipped."""
    cutoff = now - _dt.timedelta(days=days)
    out = []
    for entry in event_log:
        if not isinstance(entry, dict):
            continue
        ts = _parse_ts(entry.get("ts"))
        if ts is not None and ts >= cutoff:
            out.append(entry)
    return out


def _of_topic(entries: list[dict], *topics: str) -> list[dict]:
    """Window entries whose topic is one of ``topics``."""
    wanted = set(topics)
    return [e for e in entries if e.get("topic") in wanted]


def _latest(entries: list[dict]) -> dict | None:
    """The newest entry by timestamp (entries are otherwise append-ordered)."""
    dated = [(_parse_ts(e.get("ts")), e) for e in entries]
    dated = [(ts, e) for ts, e in dated if ts is not None]
    if not dated:
        return entries[-1] if entries else None
    return max(dated, key=lambda pair: pair[0])[1]


def _section_habits(entries: list[dict]) -> str | None:
    """💪 Habit-nudge activity for the week.

    Habit *completion* is not retained in state (the habits topic keeps only the
    current day's sent slots), so the honest weekly signal is how many nudges
    each habit fired — pulled from the event log. Skipped when none fired.
    """
    habit_entries = _of_topic(entries, "habits")
    if not habit_entries:
        return None
    by_name = Counter(e.get("source") or "habit" for e in habit_entries)
    nudges = ", ".join(f"{name} ({n})" for name, n in by_name.most_common())
    return f"💪 HABITS\n- Habit nudges sent: {nudges}"


def _fx_line(state: dict) -> str | None:
    """Week-over-week USD/DOP move from the fx state, or None if not yet tracked."""
    rate = state.get("fx_last_rate")
    baseline = state.get("fx_week_baseline") or {}
    base_rate = baseline.get("rate") if isinstance(baseline, dict) else None
    if rate is None or base_rate is None:
        return None
    try:
        rate = float(rate)
        base_rate = float(base_rate)
    except (TypeError, ValueError):
        return None
    delta = rate - base_rate
    pct = (delta / base_rate * 100) if base_rate else 0.0
    if abs(delta) < 0.005:
        return f"- USD/DOP: held steady at {rate:.2f}"
    return f"- USD/DOP: {base_rate:.2f} -> {rate:.2f} ({delta:+.2f}, {pct:+.2f}%)"


def _upcoming_bills(today: _dt.date, bills: list[dict] | None = None,
                    days: int = WINDOW_DAYS) -> list[str]:
    """Bills due within ``days`` of ``today``, soonest first.

    Reads ``reminders.json`` via the bills topic by default (so the due days stay
    a single source of truth); tests inject ``bills`` to stay file-independent.
    """
    if bills is None:
        bills = bills_topic._load()
    out: list[tuple[_dt.date, str]] = []
    for b in bills:
        name = b.get("name")
        try:
            due_day = int(b.get("due_day"))
        except (ValueError, TypeError):
            continue
        if not name or not 1 <= due_day <= 31:
            continue
        occ = bills_topic._next_due(due_day, today)
        days_left = (occ - today).days
        if 0 <= days_left <= days:
            when = "today" if days_left == 0 else (
                "tomorrow" if days_left == 1 else f"in {days_left} days")
            out.append((occ, f"{name} due {occ.isoformat()} ({when})"))
    return [line for _, line in sorted(out)]


def _section_finance(state: dict, entries: list[dict], today: _dt.date) -> str | None:
    """💰 Spending recap + weekly FX move + bills due in the next 7 days."""
    lines: list[str] = []

    spending = _of_topic(entries, "spending")
    if spending:
        latest = _latest(spending)
        detail = (latest.get("detail") or "").strip().splitlines()
        summary = detail[0] if detail else (latest.get("title") or "").strip()
        if summary:
            lines.append(f"- Spending: {summary}")

    fx = _fx_line(state)
    if fx:
        lines.append(fx)

    bills = _upcoming_bills(today)
    if bills:
        lines.append(f"- Upcoming bills: {'; '.join(bills)}")

    if not lines:
        return None
    return "💰 FINANCE\n" + "\n".join(lines)


def _section_weather(entries: list[dict]) -> str | None:
    """🌤 Weather alerts logged + UV/air-quality warnings from the past week."""
    lines: list[str] = []

    weather = _of_topic(entries, *_WEATHER_TOPICS)
    if weather:
        latest = _latest(weather)
        title = (latest.get("title") or "").strip()
        suffix = f" (latest: {title})" if title else ""
        lines.append(f"- Weather alerts logged: {len(weather)}{suffix}")

    env = _of_topic(entries, *_ENV_TOPICS)
    if env:
        latest = _latest(env)
        title = (latest.get("title") or "").strip()
        suffix = f" (latest: {title})" if title else ""
        lines.append(f"- UV / air-quality warnings: {len(env)}{suffix}")

    if not lines:
        return None
    return "🌤 WEATHER & ENVIRONMENT\n" + "\n".join(lines)


def _section_entertainment(entries: list[dict]) -> str | None:
    """🎬 Top movie/game release of the week + Golden Sun and AI news that fired."""
    lines: list[str] = []

    releases = _of_topic(entries, *_RELEASE_TOPICS)
    if releases:
        top = max(releases, key=lambda e: int(e.get("score") or 0))
        title = (top.get("title") or "").strip()
        if title:
            lines.append(f"- Top release: [{int(top.get('score') or 0)}] {title}")

    for topic, label in (("golden_sun", "Golden Sun"), ("anthropic_news", "AI news")):
        items = _of_topic(entries, topic)
        if not items:
            continue
        latest = (_latest(items).get("title") or "").strip()
        suffix = f'; latest: "{latest}"' if latest else ""
        lines.append(f"- {label}: {len(items)} item(s){suffix}")

    if not lines:
        return None
    return "🎬 ENTERTAINMENT & NEWS\n" + "\n".join(lines)


def _errored_this_week(health: dict, now: _dt.datetime, days: int = WINDOW_DAYS) -> list[str]:
    """Topics whose topic_health shows an error stamped within the window.

    ``topic_health`` records only the *last* error (no per-week count), so we
    surface topics currently in an error state that failed within the past week
    — the best available proxy for "errored this week".
    """
    cutoff = now - _dt.timedelta(days=days)
    out = []
    for name, entry in (health or {}).items():
        if not isinstance(entry, dict) or not entry.get("last_error"):
            continue
        ts = _parse_ts(entry.get("last_error_ts"))
        if ts is None or ts >= cutoff:
            out.append(name)
    return sorted(out)


def _section_system(entries: list[dict], health: dict, now: _dt.datetime) -> str | None:
    """⚙️ Topics that errored this week + total live pushes sent this week."""
    lines: list[str] = []

    failing = _errored_this_week(health, now)
    if failing:
        lines.append(f"- Topics with errors this week: {', '.join(failing)}")

    pushed = sum(1 for e in entries if e.get("action") == "push")
    if pushed:
        lines.append(f"- {pushed} notification(s) pushed this week")

    if not lines:
        return None
    return "⚙️ SYSTEM HEALTH\n" + "\n".join(lines)


def _compile(state: dict, entries: list[dict], now: _dt.datetime) -> list[str]:
    """Build every non-empty section, in display order."""
    today = now.date()
    health = state.get("topic_health") or {}
    sections = [
        _section_habits(entries),
        _section_finance(state, entries, today),
        _section_weather(entries),
        _section_entertainment(entries),
        _section_system(entries, health, now),
    ]
    return [s for s in sections if s]


def run(state: dict) -> dict:
    """Compile and send the Sunday life dashboard once per ISO week.

    Rides the daily run (like recap), but acts only on Sunday — the natural
    end of the ISO week, so the digest covers a full Mon-Sun span. Deduped per
    ISO week so the day's repeated post-threshold runs never double-send; a week
    with no data anywhere stays silent but is still stamped.
    """
    if not os.environ.get("NOTIFY_DAILY"):
        return state  # weekly work rides the daily run, like recap/games
    now = _utcnow()
    today = now.date()
    if today.weekday() != SUNDAY:
        return state  # the dashboard is a Sunday digest
    week = _iso_week(today)
    if state.get(STATE_KEY) == week:
        return state

    entries = _window(state.get(EVENT_LOG_KEY) or [], now)
    sections = _compile(state, entries, now)
    if not sections:
        log.info("life dashboard: no data this week; skipping for %s", week)
        state[STATE_KEY] = week
        return state

    state = events.emit(
        state,
        title="Your week in review",
        body="\n\n".join(sections),
        topic="life_dashboard",
        severity="low",
        source="Life Dashboard",
        tags="calendar",
        legacy_priority="low",
        legacy_action="push",
    )
    log.info("life dashboard: sent weekly digest for %s (%d section(s))",
             week, len(sections))
    state[STATE_KEY] = week
    return state
