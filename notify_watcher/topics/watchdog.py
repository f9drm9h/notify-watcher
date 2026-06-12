"""Topic: watchdog — self-monitoring over ``state["topic_health"]`` (no network).

main.py already stamps every topic's last successful run (``last_ok``) and most
recent failure (``last_error`` / ``last_error_ts``) into ``state["topic_health"]``,
but nothing reads it: a feed that dies (URL moved, API key revoked, source gone)
just fails silently every run, forever. This topic closes that loop — when some
topic has been failing with no successful run for ``stale_hours`` (monitors.json
-> watchdog, default 48), it pushes ONE heads-up naming the topic, how long it
has been down, and its last error.

Failures land in ``last_error`` two ways and this check treats them alike: a
topic that RAISED (main.py's except), and a topic that swallowed its own source
failure but reported it via the topic health contract (health.source_failed —
main.py records it as a soft failure WITHOUT stamping ``last_ok``, and a later
no-claim run leaves it sticky). So a direct scraper whose source dies quietly
now crosses this same threshold instead of looking healthy forever.

Once per outage: an alerted topic is remembered in ``state["watchdog_alerted"]``
and not re-alerted until it recovers (main.py clears ``last_error`` on the next
success), which re-arms it for a future outage. A topic that has NEVER succeeded
has no ``last_ok`` to measure from, so the first time the watchdog observes it
failing it records that moment in ``state["watchdog_failing_since"]`` and the
stale clock runs from there. Simultaneous outages (e.g. a runner-wide network
blip taking several topics past the threshold on the same run) are bundled into
a single push rather than one per topic.

A second, opt-in check covers the failure the error stamps can't see: a scraper
that gets HTTP 200 but parses zero items forever ("successfully doing nothing"
stamps a healthy ``last_ok`` every run). The collector and news engines stamp
``topic_health[topic]["last_data"]`` whenever a fetch returns at least one item
(monitor.stamp_last_data); for each topic listed in monitors.json ->
``watchdog.data_stale_days`` ({topic: days}), this alerts once — same
once-per-outage/re-arm pattern — when no data has been seen for that many days.
Unconfigured topics are never data-checked, because only the per-topic config
knows "quiet spell" from "broken": opt in only sources that produce items near
daily. A configured topic with no stamp yet starts its clock at first
observation (``state["watchdog_data_baseline"]``), so enabling the check never
alerts instantly.

Both alert kinds persist their "already alerted" marker only AFTER the push
succeeds: if ntfy itself is down, the run fails, the marker is not written, and
the next run re-sends — otherwise the watchdog's own alert could be lost
exactly when the transport is broken.

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
DATA_BASELINE_KEY = "watchdog_data_baseline"
DATA_ALERTED_KEY = "watchdog_data_alerted"
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


def _evaluate_data(
    health: dict,
    stale_days: dict,
    baseline: dict,
    alerted: dict,
    now: _dt.datetime,
) -> tuple[list[tuple[str, _dt.datetime, float]], dict, dict]:
    """Pure. Returns ``(alerts, baseline, alerted)`` for the data-staleness check.

    ``alerts`` lists ``(topic, last_seen, days)`` for every configured topic whose
    last ``last_data`` stamp (or, if it has never stamped, the moment this check
    first observed it — the baseline) is older than its configured ``days`` and
    that has not been alerted this outage. The returned dicts are rebuilt from
    the current config, so a topic removed from ``data_stale_days`` drops its
    tracking, and a topic whose data is fresh again is re-armed by simply not
    carrying its alerted marker forward. Invalid day values skip the topic; an
    unparseable baseline restarts that topic's clock rather than guessing.
    """
    bl: dict = {}
    al: dict = {}
    alerts: list[tuple[str, _dt.datetime, float]] = []
    for name in sorted(stale_days):
        try:
            days = float(stale_days[name])
        except (TypeError, ValueError):
            continue
        if days <= 0:
            continue
        bl[name] = baseline.get(name) or now.isoformat()
        entry = health.get(name)
        last = _parse(entry.get("last_data")) if isinstance(entry, dict) else None
        anchor = last or _parse(bl[name])
        if anchor is None:
            bl[name] = now.isoformat()
            continue
        if now - anchor < _dt.timedelta(days=days):
            continue  # fresh (or clock still young): not stale, and re-armed
        if name in alerted:
            al[name] = alerted[name]  # still down, already alerted this outage
            continue
        alerts.append((name, anchor, days))
        al[name] = now.isoformat()
    return alerts, bl, al


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


def _build_data_message(alerts: list[tuple[str, _dt.datetime, float]]) -> tuple[str, str]:
    """Pure. Render (title, body) for one bundled data-staleness push."""
    if len(alerts) == 1:
        name, _, days = alerts[0]
        title = f"Watchdog: '{name}' has produced no data in {days:g}+ days"
    else:
        title = f"Watchdog: {len(alerts)} topics have produced no data recently"
    lines = []
    for name, anchor, days in alerts:
        lines.append(
            f"{name} — no items seen since {_fmt_ts(anchor)} (threshold {days:g}d); "
            "the source may have silently changed its format"
        )
    return title, "\n".join(lines)


def _emit_alert(state: dict, title: str, body: str) -> dict:
    """One watchdog push. Raises on transport failure (callers rely on that)."""
    log.warning("watchdog: %s", title)
    return events.emit(
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


def _check_data(state: dict, health: dict, cfg: dict, now: _dt.datetime) -> dict:
    """Opt-in "no data for N days" check over the last_data stamps."""
    stale_days = cfg.get("data_stale_days")
    if not isinstance(stale_days, dict) or not stale_days:
        return state
    alerts, bl, al = _evaluate_data(
        health,
        stale_days,
        state.get(DATA_BASELINE_KEY) or {},
        state.get(DATA_ALERTED_KEY) or {},
        now,
    )
    state[DATA_BASELINE_KEY] = bl
    if alerts:
        state = _emit_alert(state, *_build_data_message(alerts))
    # Persisted only after a successful emit, same as ALERTED_KEY below: a
    # failed push leaves the marker unwritten so the next run re-sends.
    state[DATA_ALERTED_KEY] = al
    return state


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

    if alerts:
        state = _emit_alert(state, *_build_message(alerts, stale_hours))
    else:
        log.info("watchdog: %d topic(s) tracked, no stale outage", len(health))
    # Persist the alerted set only AFTER the emit returned: if the push itself
    # failed (ntfy outage), the exception above leaves this unwritten, main.py
    # records the watchdog as failed, and the next run re-evaluates and
    # re-sends. Before this reorder the alert was marked sent even when it
    # never left the runner.
    state[ALERTED_KEY] = al

    return _check_data(state, health, cfg, now)
