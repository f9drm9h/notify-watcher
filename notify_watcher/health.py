"""Topic health contract: report a source outcome without crashing the run.

Every direct scraper swallows its own fetch failures (log + ``return state``)
so one dead source never kills the sweep — but that made "source is down"
indistinguishable from "ran fine": main.py stamped ``last_ok`` for any topic
that didn't raise, so the watchdog's no-successful-run check could never fire
for a swallowed failure. This module is the fix: a topic *reports* what its
source actually did, and main.py stamps ``topic_health`` from that report.

A topic that adopts the contract (its name is in ``ADOPTED``) calls exactly one
of these per run, at the point where it knows the outcome:

    health.source_ok(state, TOPIC, data_count=N)   # source answered; N items
    health.source_failed(state, TOPIC, "why")      # every source path failed

and stays silent on runs where it makes no claim (daily-gated topic on a
3-hourly run, nothing configured). main.py then:

  - stamps ``last_ok`` only for an explicit ok report (true success),
  - records a soft failure as ``last_error``/``last_error_ts`` plus a
    ``source_failed`` marker — the same shape the watchdog already alerts on,
  - leaves the topic_health entry COMPLETELY untouched on a no-claim run, so a
    soft failure stays sticky until a true success instead of being wiped by
    the next gated no-op run.

``source_ok`` with ``data_count > 0`` also stamps ``last_data`` (via
monitor.stamp_last_data), giving the direct scrapers the same data-staleness
coverage the collector/news engines already have.

The report itself lives under the transient ``state["_topic_status"]`` key:
main.py consumes each topic's entry right after the topic runs and pops the
whole key before saving, so it never reaches state.json.
"""
from __future__ import annotations

from . import monitor

# Transient per-run scratch: {topic: status dict}. Consumed by main.py, never
# persisted (main pops it before state_mod.save).
STATUS_KEY = "_topic_status"

# Topics that have adopted the contract. For these, main.py stamps health only
# from explicit reports; "didn't raise" alone proves nothing about the source.
# Keep in sync with the topics module: each name here must call source_ok /
# source_failed on every path that actually contacted its source.
ADOPTED = frozenset({
    "deals",
    "fuel",
    "fx",
    "groceries",
    "onamet",
    "outages",
    "quakes",
    "twitch",
    "weather",
    "youtube",
})

_MESSAGE_LEN = 300


def topic_status(state: dict, topic: str, *, ok: bool,
                 data_count: int = 0, message: str = "") -> None:
    """Record this run's source outcome for ``topic``.

    Call once per run, when the topic knows what its source did. A later call
    in the same run overwrites the earlier one, so multi-source topics should
    aggregate first and report once. An ok report with ``data_count > 0`` also
    stamps ``topic_health[topic]["last_data"]`` for the watchdog's opt-in
    data-staleness check.
    """
    if not topic:
        return
    state.setdefault(STATUS_KEY, {})[topic] = {
        "ok": bool(ok),
        "source_failed": not ok,
        "data_count": int(data_count),
        "message": str(message)[:_MESSAGE_LEN],
    }
    if ok and data_count > 0:
        monitor.stamp_last_data(state, topic, data_count)


def source_ok(state: dict, topic: str, data_count: int = 0,
              message: str = "") -> None:
    """The source answered and parsed; ``data_count`` items were observed."""
    topic_status(state, topic, ok=True, data_count=data_count, message=message)


def source_failed(state: dict, topic: str, message: str) -> None:
    """Every path to the source failed (fetch error, parse-to-zero, dead key)."""
    topic_status(state, topic, ok=False, message=message)


def consume(state: dict, topic: str) -> dict | None:
    """Pop and return ``topic``'s status report for this run (None if silent)."""
    reports = state.get(STATUS_KEY)
    if not isinstance(reports, dict):
        return None
    return reports.pop(topic, None)
