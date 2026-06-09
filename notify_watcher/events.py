"""Normalize every topic's notification into a common Event and route it.

``emit`` is the single funnel the Personal Priority Engine adds *above* the dumb
ntfy transport. A topic builds an Event — title, body, topic, severity, source,
timestamp, metadata — and calls ``emit``; the engine (``priority.decide``)
scores it cross-topic and routes:

    push   -> ntfy.push now, at the banded ntfy priority
    digest -> digest.add (buffered for the daily flush), carrying the GLOBAL
              priority score so the digest ranks/evicts by cross-topic priority
              with no change to digest.py
    drop   -> nothing

THE BACKWARD-COMPAT KEYSTONE: when monitors.json has no ``priority`` section the
engine is OFF (``priority.decide`` returns None) and ``emit`` falls back to
LEGACY routing — it pushes (or digests) exactly as the caller's pre-engine code
did. The caller states its legacy behavior explicitly via ``legacy_action`` /
``legacy_priority`` / ``score``. So converting a topic from a raw ``ntfy.push``
to ``emit`` is behavior-preserving until the config is authored, and the full
existing test suite stays green.

``emit`` references ntfy/digest/config via the module objects (``from . import
…``), so tests that patch ``ntfy.push`` (see tests/_util.capture_pushes) see the
patched function, exactly as the collector engine does.
"""
from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field
from typing import Optional

from . import config, digest, ntfy, priority

log = logging.getLogger(__name__)

# Ordered severity vocabulary (low -> high). Collector tiers map onto this:
# breakthrough -> "critical", high -> "high", moderate -> "moderate".
SEVERITIES = ("info", "low", "moderate", "high", "critical")


@dataclass(frozen=True)
class Event:
    """A normalized notification, identical in shape across every topic."""
    title: str
    body: str
    topic: str
    severity: str
    source: str
    timestamp: str
    metadata: dict = field(default_factory=dict)


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _push(event: Event, ntfy_priority: Optional[str]) -> None:
    """Send one event via the ntfy transport, reading click/tags from metadata.

    A ``title_prefix`` metadata hint (set by the collector engine) renders the
    label-style push the collectors have always sent — bold ``"<prefix>: <source>"``
    Title with the headline (``event.title``) as the message — so migrating the
    collectors to ``emit`` is byte-for-byte identical. Without the hint (direct
    topics) the push is the plain Title=event.title / message=event.body.
    """
    prefix = event.metadata.get("title_prefix")
    if prefix:
        title = f"{prefix}: {event.source}".strip(": ")
        message = event.title
    else:
        title = event.title
        message = event.body
    ntfy.push(
        title=title,
        message=message,
        click_url=event.metadata.get("click_url") or None,
        tags=event.metadata.get("tags") or None,
        priority=ntfy_priority,
    )


def _to_digest(state: dict, event: Event, score: int, digest_cfg: Optional[dict]) -> None:
    """Buffer one event for the daily digest, storing `score` for ranking/eviction."""
    if digest_cfg is None:
        digest_cfg = config.section("digest")
    digest.add(
        state,
        {
            "title": event.title,
            "url": event.metadata.get("click_url", "") or "",
            "source": event.source,
            "score": int(score),
        },
        digest_cfg,
    )


def emit(
    state: dict,
    *,
    title: str,
    topic: str,
    body: str = "",
    severity: str = "moderate",
    source: str = "",
    metadata: Optional[dict] = None,
    click_url: Optional[str] = None,
    tags: Optional[str] = None,
    legacy_priority: Optional[str] = None,
    legacy_action: str = "push",
    score: int = 0,
    priority_cfg: Optional[dict] = None,
    digest_cfg: Optional[dict] = None,
) -> dict:
    """Normalize a notification into an Event, route it, and return state.

    Engine ON (a ``priority`` section exists): the global score decides push vs.
    digest vs. drop, and a push uses the banded ntfy priority.

    Engine OFF (no section): LEGACY routing reproduces the caller's pre-engine
    behavior — ``legacy_action="push"`` sends ``ntfy.push`` at ``legacy_priority``
    (the old call's priority, or None for the server default); ``"digest"``
    buffers the item at the caller's within-domain ``score``.

    ``click_url`` and ``tags`` are transport hints; they are folded into the
    Event's metadata so the Event stays the single normalized source of truth.
    ``priority_cfg`` / ``digest_cfg`` default to the monitors.json sections and
    exist mainly so tests can inject synthetic config.
    """
    md = dict(metadata or {})
    if click_url is not None:
        md.setdefault("click_url", click_url)
    if tags is not None:
        md.setdefault("tags", tags)

    event = Event(
        title=title,
        body=body,
        topic=topic,
        severity=severity,
        source=source,
        timestamp=_now_iso(),
        metadata=md,
    )

    if priority_cfg is None:
        priority_cfg = config.section("priority")

    try:
        decision = priority.decide(event, priority_cfg)
    except Exception as exc:  # noqa: BLE001 - engine errors must fail open to legacy
        log.error("priority engine failed for %s/%r; using legacy routing: %s",
                  topic, title, exc)
        decision = None

    if decision is None:
        # Engine OFF -> reproduce the caller's exact pre-engine behavior.
        if legacy_action == "digest":
            _to_digest(state, event, score, digest_cfg)
        else:
            _push(event, legacy_priority)
        return state

    if decision.action == "push":
        _push(event, decision.ntfy_priority)
    elif decision.action == "digest":
        _to_digest(state, event, decision.score, digest_cfg)
    # "drop" -> intentionally nothing
    return state
