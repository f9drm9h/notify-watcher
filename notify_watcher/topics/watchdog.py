"""Topic: watchdog — self-monitoring over ``state["topic_health"]`` (no network).

main.py stamps every topic's last successful run (``last_ok``) and most recent
failure (``last_error`` / ``last_error_ts``) into ``state["topic_health"]``.
This topic is what reads those stamps: a feed that dies (URL moved, API key
revoked, source gone) must not fail silently every run, forever.

Failures land in ``last_error`` two ways and this check treats them alike: a
topic that RAISED (main.py's except), and a topic that swallowed its own source
failure but reported it via the topic health contract (health.source_failed —
main.py records it as a soft failure WITHOUT stamping ``last_ok``, and a later
no-claim run leaves it sticky). So a direct scraper whose source dies quietly is
seen here instead of looking healthy forever.

OUTAGE LIFECYCLE (the reliability-audit redesign; was: one alert per outage,
then silence until recovery — which meant a long outage looked identical to a
resolved one after the first push, and recovery was never announced at all):

  1. FIRST ALERT — pushed on the first run where a topic is failing. Waiting
     ``alert_delay_hours`` first is possible (monitors.json -> watchdog) but the
     default is 0: a broken monitor should be reported when it breaks, not two
     days later.
  2. REMINDERS — while the outage continues, one more push every
     ``reminder_hours`` (default 12). This is the piece that makes the watchdog
     trustworthy: silence now means "healthy", not "already told you once".
     Set ``reminder_hours`` to 0 to disable reminders.
  3. RECOVERY — EXACTLY ONE green push when the topic succeeds again, and only
     if the outage was actually alerted on. An outage that recovered inside the
     grace window is dropped silently; nothing was ever reported, so there is
     nothing to close out.

Anti-spam is structural, not a special case: every topic crossing the same
transition on the same run is bundled into ONE push, so a runner-wide network
blip taking 20 topics down produces one message, not 20. At most three pushes
can leave one run per check (new / reminder / recovery), regardless of how many
topics are involved.

A second, opt-in check covers the failure the error stamps can't see: a scraper
that gets HTTP 200 but parses zero items forever ("successfully doing nothing"
stamps a healthy ``last_ok`` every run). The collector and news engines stamp
``topic_health[topic]["last_data"]`` whenever a fetch returns at least one item
(monitor.stamp_last_data); for each topic listed in monitors.json ->
``watchdog.data_stale_days`` ({topic: days}), a stale stamp runs through the
same three-phase lifecycle, with reminders paced in days
(``data_reminder_days``, default 7) because those outages are slow by nature.
Unconfigured topics are never data-checked, because only the per-topic config
knows "quiet spell" from "broken": opt in only sources that produce items near
daily. A configured topic with no stamp yet starts its clock at first
observation (``state["watchdog_data_baseline"]``), so enabling the check never
alerts instantly.

DELIVERY CONTRACT: each bundle's state markers are written only AFTER that
bundle's push returned. If Discord is down the push raises, the marker is not
written, main.py records the watchdog as failed, and the next run re-sends —
at-least-once, never at-most-once. A watchdog alert must not be lost exactly
when the transport is broken. The three bundles are marked independently, so a
failure delivering the recovery notice cannot roll back the outage alert and
make it fire twice.

Outage alerts are emitted at ``critical`` severity so ``MUTE:watchdog`` cannot
downgrade them into the digest (events._apply_mute exempts critical) — muting
the thing that tells you your monitors are broken is never what the button
meant. Recovery notices stay ``high``; they are good news and can wait.

Runs every cycle (cheap, pure state inspection) and is registered LAST in
main.TOPICS so it sees the current run's health for every other topic. Needs no
first-run seeding: nothing alerts until a topic actually reports a failure.
"""
from __future__ import annotations

import datetime as _dt
import logging

from .. import config, events

log = logging.getLogger(__name__)

TOPIC = "watchdog"
# Current outage ledger: {topic: {"since", "alerted", "last_alert", "reminders"}}.
OUTAGES_KEY = "watchdog_outages"
DATA_OUTAGES_KEY = "watchdog_data_outages"
DATA_BASELINE_KEY = "watchdog_data_baseline"
# Pre-redesign keys, read once for migration then dropped (see _migrate).
LEGACY_FAILING_SINCE_KEY = "watchdog_failing_since"
LEGACY_ALERTED_KEY = "watchdog_alerted"
LEGACY_DATA_ALERTED_KEY = "watchdog_data_alerted"

DEFAULT_ALERT_DELAY_HOURS = 0.0
DEFAULT_REMINDER_HOURS = 12.0
DEFAULT_DATA_REMINDER_DAYS = 7.0
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


def _fmt_age(delta: _dt.timedelta) -> str:
    """Compact human duration: '40m', '7h', '3d 4h'."""
    total = max(int(delta.total_seconds()), 0)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _num(cfg: dict, key: str, default: float, *, fallback_key: str = "") -> float:
    """Read a numeric config knob, tolerating a missing/garbage value."""
    raw = cfg.get(key)
    if raw is None and fallback_key:
        raw = cfg.get(fallback_key)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        log.warning("watchdog: ignoring non-numeric %s=%r", key, raw)
        return default


class Transition:
    """One topic's place in an outage, as handed to the message builders.

    ``anchor`` is what the message dates the outage from — the last successful
    run when the topic ever had one, else the moment the watchdog first saw it
    failing. ``detail`` is the last error (outage check) or the configured
    staleness window (data check).
    """

    __slots__ = ("name", "anchor", "detail", "reminders")

    def __init__(self, name: str, anchor: _dt.datetime, detail: str, reminders: int = 0):
        self.name = name
        self.anchor = anchor
        self.detail = detail
        self.reminders = reminders

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"Transition({self.name!r}, {self.anchor!r}, "
                f"{self.detail!r}, {self.reminders})")

    def __eq__(self, other) -> bool:
        return (isinstance(other, Transition)
                and (self.name, self.anchor, self.detail, self.reminders)
                == (other.name, other.anchor, other.detail, other.reminders))


def _track(
    failing: dict[str, dict],
    tracked: dict,
    now: _dt.datetime,
    *,
    delay: _dt.timedelta,
    reminder: _dt.timedelta | None,
) -> tuple[list[Transition], list[Transition], list[Transition], dict]:
    """Pure outage state machine. Returns ``(new, reminders, recoveries, ledger)``.

    ``failing`` is what is broken RIGHT NOW: ``{topic: {"detail": str,
    "anchor": datetime|None}}``. ``tracked`` is the persisted ledger from the
    previous run. The returned ``ledger`` carries every still-failing topic with
    its outage start stamped, but with NO alert markers advanced — the caller
    stamps those itself, per bundle, only after that bundle's push succeeded
    (see the delivery contract in the module docstring).

    A topic recovers by disappearing from ``failing``; it produces a recovery
    Transition only if its outage had actually been alerted on.
    """
    ledger: dict = {}
    new: list[Transition] = []
    due: list[Transition] = []
    recovered: list[Transition] = []

    for name in sorted(failing):
        report = failing[name]
        prev = tracked.get(name)
        prev = prev if isinstance(prev, dict) else {}
        since = prev.get("since") or now.isoformat()
        entry = {"since": since, "reminders": int(prev.get("reminders") or 0)}
        if prev.get("alerted"):
            entry["alerted"] = prev["alerted"]
            entry["last_alert"] = prev.get("last_alert") or prev["alerted"]
        ledger[name] = entry

        anchor = report.get("anchor") or _parse(since) or now
        detail = str(report.get("detail") or "")
        if not entry.get("alerted"):
            if now - (_parse(since) or now) >= delay:
                new.append(Transition(name, anchor, detail))
            continue
        if reminder is None:
            continue
        last = _parse(entry.get("last_alert")) or now
        if now - last >= reminder:
            due.append(Transition(name, anchor, detail, entry["reminders"] + 1))

    for name in sorted(tracked):
        prev = tracked[name]
        if name in failing or not isinstance(prev, dict) or not prev.get("alerted"):
            # Not alerted on -> nothing to close out, so it just leaves the
            # ledger: an outage nobody heard about needs no all-clear.
            continue
        # A recovering topic STAYS in the ledger until its notice is delivered;
        # _deliver pops it only after the push returns. Dropping it here would
        # lose the recovery entirely if Discord happened to be down.
        ledger[name] = prev
        recovered.append(Transition(name, _parse(prev.get("since")) or now, ""))

    return new, due, recovered, ledger


def _migrate(state: dict) -> dict:
    """One-time lift of the pre-redesign keys into the new outage ledger.

    The old shape was two parallel dicts — ``watchdog_failing_since`` (when the
    outage started) and ``watchdog_alerted`` (when the single alert went out).
    Folding them in keeps an in-flight outage from re-alerting on the deploy run
    and, more importantly, keeps its recovery notice: a topic that was already
    alerted stays alerted, so its comeback still closes the loop.
    """
    ledger = state.get(OUTAGES_KEY)
    if isinstance(ledger, dict):
        return ledger
    since_map = state.get(LEGACY_FAILING_SINCE_KEY) or {}
    alerted_map = state.get(LEGACY_ALERTED_KEY) or {}
    if not isinstance(alerted_map, dict):
        alerted_map = {}
    ledger = {}
    if isinstance(since_map, dict):
        for name, since in since_map.items():
            entry = {"since": since, "reminders": 0}
            was = alerted_map.get(name)
            if was:
                entry["alerted"] = was
                entry["last_alert"] = was
            ledger[name] = entry
    if ledger:
        log.info("watchdog: migrated %d outage(s) from the legacy state keys",
                 len(ledger))
    return ledger


def _migrate_data(state: dict) -> dict:
    """Same one-time lift for the data-staleness check's alerted markers."""
    ledger = state.get(DATA_OUTAGES_KEY)
    if isinstance(ledger, dict):
        return ledger
    alerted_map = state.get(LEGACY_DATA_ALERTED_KEY) or {}
    ledger = {}
    if isinstance(alerted_map, dict):
        for name, when in alerted_map.items():
            ledger[name] = {"since": when, "alerted": when,
                            "last_alert": when, "reminders": 0}
    return ledger


def _drop_legacy(state: dict) -> None:
    for key in (LEGACY_FAILING_SINCE_KEY, LEGACY_ALERTED_KEY, LEGACY_DATA_ALERTED_KEY):
        state.pop(key, None)


# --- message rendering ------------------------------------------------------

def _snippet(text: str) -> str:
    if len(text) <= _ERROR_SNIPPET_LEN:
        return text
    return text[: _ERROR_SNIPPET_LEN - 1] + "…"


def _build_message(alerts: list[Transition], now: _dt.datetime) -> tuple[str, str]:
    """Pure. (title, body) for the FIRST alert of one or more outages."""
    if len(alerts) == 1:
        title = f"Watchdog: '{alerts[0].name}' is failing"
    else:
        title = f"Watchdog: {len(alerts)} topics are failing"
    lines = [
        f"{a.name} — no successful run since {_fmt_ts(a.anchor)} "
        f"({_fmt_age(now - a.anchor)}); last error: {_snippet(a.detail)}"
        for a in alerts
    ]
    return title, "\n".join(lines)


def _build_reminder(alerts: list[Transition], now: _dt.datetime) -> tuple[str, str]:
    """Pure. (title, body) for a still-down reminder."""
    if len(alerts) == 1:
        a = alerts[0]
        title = f"Watchdog: '{a.name}' is STILL failing ({_fmt_age(now - a.anchor)})"
    else:
        title = f"Watchdog: {len(alerts)} topics are STILL failing"
    lines = [
        f"{a.name} — down {_fmt_age(now - a.anchor)} (since {_fmt_ts(a.anchor)}), "
        f"reminder #{a.reminders}; last error: {_snippet(a.detail)}"
        for a in alerts
    ]
    return title, "\n".join(lines)


def _build_recovery(alerts: list[Transition], now: _dt.datetime) -> tuple[str, str]:
    """Pure. (title, body) for the single recovery notice."""
    if len(alerts) == 1:
        title = f"Watchdog: '{alerts[0].name}' recovered"
    else:
        title = f"Watchdog: {len(alerts)} topics recovered"
    lines = [f"{a.name} — running again after {_fmt_age(now - a.anchor)} down"
             for a in alerts]
    return title, "\n".join(lines)


def _build_data_message(alerts: list[Transition], now: _dt.datetime) -> tuple[str, str]:
    if len(alerts) == 1:
        title = (f"Watchdog: '{alerts[0].name}' has produced no data "
                 f"in {alerts[0].detail}")
    else:
        title = f"Watchdog: {len(alerts)} topics have produced no data recently"
    lines = [
        f"{a.name} — no items seen since {_fmt_ts(a.anchor)} "
        f"(threshold {a.detail}); the source may have silently changed its format"
        for a in alerts
    ]
    return title, "\n".join(lines)


def _build_data_reminder(alerts: list[Transition], now: _dt.datetime) -> tuple[str, str]:
    if len(alerts) == 1:
        a = alerts[0]
        title = f"Watchdog: '{a.name}' STILL has no data ({_fmt_age(now - a.anchor)})"
    else:
        title = f"Watchdog: {len(alerts)} topics still have no data"
    lines = [
        f"{a.name} — nothing since {_fmt_ts(a.anchor)} ({_fmt_age(now - a.anchor)}), "
        f"reminder #{a.reminders}; threshold {a.detail}"
        for a in alerts
    ]
    return title, "\n".join(lines)


def _build_data_recovery(alerts: list[Transition], now: _dt.datetime) -> tuple[str, str]:
    if len(alerts) == 1:
        title = f"Watchdog: '{alerts[0].name}' is producing data again"
    else:
        title = f"Watchdog: {len(alerts)} topics are producing data again"
    return title, "\n".join(f"{a.name} — items are flowing again" for a in alerts)


# --- emission ---------------------------------------------------------------

def _emit_alert(state: dict, title: str, body: str, *,
                severity: str = "critical") -> dict:
    """One watchdog push. Raises on transport failure (callers rely on that)."""
    log.warning("watchdog: %s", title)
    return events.emit(
        state,
        title=title,
        body=body,
        topic=TOPIC,
        severity=severity,
        source="Watchdog",
        tags="warning" if severity == "critical" else "white_check_mark",
        legacy_priority="high",
        legacy_action="push",
    )


def _deliver(
    state: dict,
    ledger: dict,
    now: _dt.datetime,
    *,
    new: list[Transition],
    due: list[Transition],
    recovered: list[Transition],
    key: str,
    render_new,
    render_reminder,
    render_recovery,
) -> dict:
    """Push each non-empty bundle and persist ITS markers right after it lands.

    Marking per bundle (rather than once at the end) is what keeps the three
    lifecycle phases independent: a Discord failure while sending the recovery
    notice must not roll back the outage alert and cause it to fire twice.
    ``state[key]`` is rewritten after every successful push, so whatever the run
    managed to deliver before an exception is durably recorded.
    """
    state[key] = ledger

    if new:
        state = _emit_alert(state, *render_new(new, now))
        stamp = now.isoformat()
        for a in new:
            entry = ledger.setdefault(a.name, {"since": stamp, "reminders": 0})
            entry["alerted"] = stamp
            entry["last_alert"] = stamp
        state[key] = ledger

    if due:
        state = _emit_alert(state, *render_reminder(due, now))
        stamp = now.isoformat()
        for a in due:
            entry = ledger.setdefault(a.name, {"since": stamp, "reminders": 0})
            entry["last_alert"] = stamp
            entry["reminders"] = a.reminders
        state[key] = ledger

    if recovered:
        state = _emit_alert(state, *render_recovery(recovered, now), severity="high")
        for a in recovered:
            ledger.pop(a.name, None)
        state[key] = ledger

    return state


# --- the two checks ---------------------------------------------------------

def _check_outages(state: dict, health: dict, cfg: dict, now: _dt.datetime) -> dict:
    """"No successful run" check over the last_error / last_ok stamps."""
    delay_hours = _num(cfg, "alert_delay_hours", DEFAULT_ALERT_DELAY_HOURS,
                       fallback_key="stale_hours")
    reminder_hours = _num(cfg, "reminder_hours", DEFAULT_REMINDER_HOURS)

    failing: dict[str, dict] = {}
    for name in sorted(health):
        entry = health[name]
        # The watchdog cannot report its own failure from inside itself; main.py
        # escalates that to a non-zero exit so alert.yml sees it instead.
        if name == TOPIC or not isinstance(entry, dict):
            continue
        error = entry.get("last_error")
        if not error:
            continue
        failing[name] = {"detail": str(error), "anchor": _parse(entry.get("last_ok"))}

    new, due, recovered, ledger = _track(
        failing,
        _migrate(state),
        now,
        delay=_dt.timedelta(hours=max(delay_hours, 0.0)),
        reminder=_dt.timedelta(hours=reminder_hours) if reminder_hours > 0 else None,
    )
    if not (new or due or recovered):
        log.info("watchdog: %d topic(s) tracked, %d failing, no state change",
                 len(health), len(failing))
    return _deliver(
        state, ledger, now,
        new=new, due=due, recovered=recovered,
        key=OUTAGES_KEY,
        render_new=_build_message,
        render_reminder=_build_reminder,
        render_recovery=_build_recovery,
    )


def _check_data(state: dict, health: dict, cfg: dict, now: _dt.datetime) -> dict:
    """Opt-in "no data for N days" check over the last_data stamps."""
    stale_days = cfg.get("data_stale_days")
    if not isinstance(stale_days, dict) or not stale_days:
        return state
    reminder_days = _num(cfg, "data_reminder_days", DEFAULT_DATA_REMINDER_DAYS)

    baseline = state.get(DATA_BASELINE_KEY)
    baseline = baseline if isinstance(baseline, dict) else {}
    fresh_baseline: dict = {}
    failing: dict[str, dict] = {}

    for name in sorted(stale_days):
        try:
            days = float(stale_days[name])
        except (TypeError, ValueError):
            continue
        if days <= 0:
            continue
        fresh_baseline[name] = baseline.get(name) or now.isoformat()
        entry = health.get(name)
        last = _parse(entry.get("last_data")) if isinstance(entry, dict) else None
        anchor = last or _parse(fresh_baseline[name])
        if anchor is None:
            # Unparseable baseline: restart this topic's clock rather than guess.
            fresh_baseline[name] = now.isoformat()
            continue
        if now - anchor < _dt.timedelta(days=days):
            continue
        failing[name] = {"detail": f"{days:g}d", "anchor": anchor}

    state[DATA_BASELINE_KEY] = fresh_baseline

    # A topic removed from data_stale_days drops its tracking entirely, so
    # re-adding it later starts clean instead of inheriting a stale marker.
    ledger_in = {k: v for k, v in _migrate_data(state).items() if k in fresh_baseline}
    new, due, recovered, ledger = _track(
        failing,
        ledger_in,
        now,
        delay=_dt.timedelta(0),
        reminder=_dt.timedelta(days=reminder_days) if reminder_days > 0 else None,
    )
    # The data clock runs from the stale anchor, not from first sighting: "no
    # items since May 20" reads better than "since the run that noticed".
    for name, report in failing.items():
        if report.get("anchor") is not None:
            ledger[name]["since"] = report["anchor"].isoformat()
    return _deliver(
        state, ledger, now,
        new=new, due=due, recovered=recovered,
        key=DATA_OUTAGES_KEY,
        render_new=_build_data_message,
        render_reminder=_build_data_reminder,
        render_recovery=_build_data_recovery,
    )


def run(state: dict) -> dict:
    cfg = config.section("watchdog")
    health = state.get("topic_health")
    if not isinstance(health, dict) or not health:
        return state

    now = _dt.datetime.now(_dt.timezone.utc)
    state = _check_outages(state, health, cfg, now)
    state = _check_data(state, health, cfg, now)
    _drop_legacy(state)
    return state
