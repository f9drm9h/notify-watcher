"""Topic: watchdog — self-monitoring over ``state["topic_health"]`` (no network).

main.py already stamps every topic's last successful run (``last_ok``) and most
recent failure (``last_error`` / ``last_error_ts``) into ``state["topic_health"]``,
but nothing reads it: a feed that dies (URL moved, API key revoked, source gone)
just fails silently every run, forever. This topic closes that loop — when some
topic has been failing with no successful run for ``stale_hours`` (monitors.json
-> watchdog, default 48), it pushes ONE heads-up naming the topic, how long it
has been down, and its last error.

Once per outage: an alerted topic is remembered in ``state["watchdog_alerted"]``
and not re-alerted until it recovers (main.py clears ``last_error`` on the next
success), which re-arms it for a future outage. A topic that has NEVER succeeded
has no ``last_ok`` to measure from, so the first time the watchdog observes it
failing it records that moment in ``state["watchdog_failing_since"]`` and the
stale clock runs from there. Simultaneous outages (e.g. a runner-wide network
blip taking several topics past the threshold on the same run) are bundled into
a single push rather than one per topic.

Runs every cycle (cheap, pure state inspection) and is registered LAST in
main.TOPICS so it sees the current run's health for every other topic. Needs no
first-run seeding: nothing alerts until a failure has persisted past the
threshold.
"""
from __future__ import annotations

import datetime as _dt
import logging

from .. import config, events

log = logging.getLogger(__name__)

TOPIC = "watchdog"
FAILING_SINCE_KEY = "watchdog_failing_since"
ALERTED_KEY = "watchdog_alerted"
DEFAULT_STALE_HOURS = 48.0
_ERROR_SNIPPET_LEN = 120


def _parse(ts) -> _dt.datetime | None:
    """Parse an ISO timestamp from state; naive values are assumed UTC. None if bad."""
    if not ts:
        return None
    try:
        dt = _dt.datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt


def _fmt_ts(dt: _dt.datetime) -> str:
    return dt.astimezone(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _evaluate(
    health: dict,
    failing_since: dict,
    alerted: dict,
    now: _dt.datetime,
    stale_hours: float,
) -> tuple[list[tuple[str, _dt.datetime, str]], dict, dict]:
    """Pure. Returns ``(alerts, failing_since, alerted)``.

    ``alerts`` lists ``(topic_name, down_since, last_error)`` for every topic whose
    outage just crossed ``stale_hours`` and has not been alerted this outage. The
    returned dicts are updated copies: a recovered topic (no ``last_error``) is
    dropped from both (re-arming it), a newly-failing topic gets a
    ``failing_since`` stamp, and a newly-alerted one is added to ``alerted``.

    The outage clock runs from ``last_ok`` when the topic has ever succeeded
    (preferring the real "no successful run since" anchor), else from the first
    moment the watchdog saw it failing. Unparseable timestamps fall back the same
    way; if no anchor parses at all the topic is skipped rather than guessed at.
    """
    fs = dict(failing_since)
    al = dict(alerted)
    alerts: list[tuple[str, _dt.datetime, str]] = []
    for name in sorted(health):
        entry = health[name]
        if name == TOPIC or not isinstance(entry, dict):
            continue
        error = entry.get("last_error")
        if not error:
            # Recovered (or never failed): clear outage tracking so the NEXT
            # outage alerts again.
            fs.pop(name, None)
            al.pop(name, None)
            continue
        if name not in fs:
            fs[name] = entry.get("last_error_ts") or now.isoformat()
        if name in al:
            continue  # already alerted this outage
        anchor = _parse(entry.get("last_ok")) or _parse(fs[name])
        if anchor is None:
            continue
        if now - anchor >= _dt.timedelta(hours=stale_hours):
            alerts.append((name, anchor, str(error)))
            al[name] = now.isoformat()
    return alerts, fs, al


def _build_message(alerts: list[tuple[str, _dt.datetime, str]], stale_hours: float) -> tuple[str, str]:
    """Pure. Render (title, body) for one bundled outage push."""
    hours = int(stale_hours)
    if len(alerts) == 1:
        name, _, _ = alerts[0]
        title = f"Watchdog: '{name}' has had no successful run in {hours}h"
    else:
        title = f"Watchdog: {len(alerts)} topics have had no successful run in {hours}h"
    lines = []
    for name, anchor, error in alerts:
        snippet = error if len(error) <= _ERROR_SNIPPET_LEN else error[: _ERROR_SNIPPET_LEN - 1] + "…"
        lines.append(f"{name} — down since {_fmt_ts(anchor)}; last error: {snippet}")
    return title, "\n".join(lines)


def run(state: dict) -> dict:
    cfg = config.section("watchdog")
    stale_hours = float(cfg.get("stale_hours", DEFAULT_STALE_HOURS))
    health = state.get("topic_health")
    if not isinstance(health, dict) or not health:
        return state

    now = _dt.datetime.now(_dt.timezone.utc)
    alerts, fs, al = _evaluate(
        health,
        state.get(FAILING_SINCE_KEY) or {},
        state.get(ALERTED_KEY) or {},
        now,
        stale_hours,
    )
    state[FAILING_SINCE_KEY] = fs
    state[ALERTED_KEY] = al

    if alerts:
        title, body = _build_message(alerts, stale_hours)
        log.warning("watchdog: %s", title)
        state = events.emit(
            state,
            title=title,
            body=body,
            topic=TOPIC,
            severity="high",
            source="Watchdog",
            tags="warning",
            legacy_priority="high",
            legacy_action="push",
        )
    else:
        log.info("watchdog: %d topic(s) tracked, no stale outage", len(health))
    return state
