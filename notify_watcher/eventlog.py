"""Append-only, capped history of every routed Event — the dashboard's data source.

``emit`` routes an Event to ntfy/digest/drop and then forgets it; nothing records
*what was notified*. This module keeps that history: ``record`` appends one entry per
routed Event to a capped ring buffer inside ``state.json`` (``EVENT_LOG_KEY``), so the
project retains a durable, cross-topic log that — unlike the digest buffer — survives
the daily flush. It rides the runner's existing ``state.json`` commit, so it needs no
new storage, server, or workflow (see docs/design/02-dashboard.md).

The log is the single source the dashboard renders and the natural home for history,
counts, trends, and the priority distribution. Each entry is exactly the normalized
Event fields plus the engine's routing decision (action + global score), so the
``[NN]`` score prefix in the dashboard is ``priority.decide``'s score verbatim.

Capped (oldest dropped first) so it can never grow without bound; the cap is read from
the ``priority`` config's ``event_log_max`` (falls back to a sane default), keeping all
engine tunables in one config section.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

EVENT_LOG_KEY = "event_log"
_DEFAULT_MAX = 500


def _cap(cfg: dict | None) -> int:
    """Ring size: ``priority.event_log_max`` if set, else the default."""
    if cfg:
        try:
            n = int(cfg.get("event_log_max", _DEFAULT_MAX))
            if n > 0:
                return n
        except (TypeError, ValueError):
            pass
    return _DEFAULT_MAX


def record(state: dict, event, action: str, score: int, cfg: dict | None = None) -> dict:
    """Append one routed-Event record and trim the ring to the cap. Returns state.

    ``action`` is the routing outcome ("push" | "digest" | "drop") and ``score`` the
    global priority (``Decision.score``; 0 under legacy routing). ``detail`` carries the
    Event body — the change-summary line (docs/design/01) when a topic provides one — so
    the dashboard shows HOW a value moved with no extra plumbing.
    """
    entry = {
        "ts": event.timestamp,
        "topic": event.topic,
        "title": event.title,
        "source": event.source,
        "severity": event.severity,
        "score": int(score),
        "action": action,
        "detail": event.body,
        "url": event.metadata.get("click_url", "") or "",
    }
    buf: list = state.setdefault(EVENT_LOG_KEY, [])
    buf.append(entry)

    cap = _cap(cfg)
    if len(buf) > cap:
        # Drop the oldest overflow in one slice (append adds at most one per call,
        # but a lowered cap could require trimming several at once).
        del buf[: len(buf) - cap]
    return state
