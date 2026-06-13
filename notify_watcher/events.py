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
from typing import Optional, TYPE_CHECKING

from . import config, control, digest, eventlog, ntfy, priority

if TYPE_CHECKING:
    from .changes import Change

log = logging.getLogger(__name__)

# Ordered severity vocabulary (low -> high). Collector tiers map onto this:
# breakthrough -> "critical", high -> "high", moderate -> "moderate".
SEVERITIES = ("info", "low", "moderate", "high", "critical")

# ntfy renders at most three action buttons per notification.
MAX_BUTTONS = 3


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


def _later_label(minutes: int) -> str:
    """Human label for a LATER button: 'Remind 3h' / '1d' / '45m'."""
    if minutes % 1440 == 0:
        return f"Remind {minutes // 1440}d"
    if minutes % 60 == 0:
        return f"Remind {minutes // 60}h"
    return f"Remind {minutes}m"


def _spec_action(spec: str, event_id: str) -> Optional[dict]:
    """One declarative button spec -> a concrete ntfy action, or None.

    Specs are the small vocabulary topics (and ``control.default_buttons``
    config) use to request item-level buttons without knowing the event id:

        "read"        -> [Read later]  READ:<event_id>
        "more"        -> [Show more]   MORE:<event_id>
        "later:180"   -> [Remind 3h]   LATER:<event_id>:180

    Unknown or malformed specs return None (skipped with a log line) so a
    config typo can never break a push, and ``control.make_action`` returns
    None when the control channel is off — both fail closed to "no button".
    """
    try:
        if spec == "read":
            return control.make_action("Read later", f"READ:{event_id}")
        if spec == "more":
            return control.make_action("Show more", f"MORE:{event_id}")
        if spec.startswith("later:"):
            minutes = int(spec.split(":", 1)[1])
            if minutes > 0:
                return control.make_action(
                    _later_label(minutes), f"LATER:{event_id}:{minutes}")
    except (ValueError, AttributeError):
        pass
    log.warning("ignoring unknown button spec %r", spec)
    return None


def _build_actions(event: Event) -> Optional[list]:
    """Assemble the push's action buttons (explicit + declarative + defaults).

    Three sources, in priority order under the hard ntfy cap of MAX_BUTTONS:

      1. ``metadata["actions"]``  — fully-built v1 actions (habits' Done,
         reminders' Snooze). Forwarded untouched, always first.
      2. ``metadata["buttons"]``  — declarative specs from the topic, expanded
         with this event's log id (see _spec_action).
      3. ``control.default_buttons[topic]`` from monitors.json — per-topic
         default specs, so giving every news push a [Read later] is a config
         edit, not a code change across topics.

    Returns None when nothing applies, keeping buttonless pushes
    byte-identical to before. Config errors fail closed to "no defaults".
    """
    explicit = list(event.metadata.get("actions") or [])
    specs = list(event.metadata.get("buttons") or [])
    try:
        defaults = (config.section("control").get("default_buttons") or {})
        for spec in defaults.get(event.topic) or []:
            if spec not in specs:
                specs.append(spec)
    except Exception:  # noqa: BLE001 - bad config must never block a push
        pass

    actions = explicit[:MAX_BUTTONS]
    if specs and len(actions) < MAX_BUTTONS:
        event_id = eventlog.entry_id(event)
        for spec in specs:
            if len(actions) >= MAX_BUTTONS:
                break
            action = _spec_action(str(spec), event_id)
            if action:
                actions.append(action)
    return actions or None


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
    # Reply buttons: explicit metadata["actions"] (v1) plus declarative specs
    # and config defaults, capped at ntfy's three (see _build_actions). A push
    # with none of those stays byte-identical to before.
    actions = _build_actions(event)
    ntfy.push(
        title=title,
        message=message,
        click_url=event.metadata.get("click_url") or None,
        tags=event.metadata.get("tags") or None,
        priority=ntfy_priority,
        attach_url=event.metadata.get("attach_url") or None,
        **({"actions": actions} if actions else {}),
    )


def _quiet_defers(ntfy_priority: Optional[str]) -> bool:
    """True when quiet hours would suppress this push AND deferral is on.

    With ``quiet_hours.defer_to_digest`` (default true) an overnight low/default
    push is rerouted into the daily digest — it arrives with the morning flush
    instead of vanishing — which is what makes quiet hours safe to enable.
    Setting ``defer_to_digest: false`` restores the old hard-drop behavior (the
    transport's own check still drops it). high/urgent never suppress, so they
    are never deferred either. Fails open (no deferral) on any config error.
    """
    try:
        if not config.section("quiet_hours").get("defer_to_digest", True):
            return False
        return ntfy.would_suppress(ntfy_priority)
    except Exception:  # noqa: BLE001 - deferral must never break a push
        return False


def _mute_active(state: dict, topic: str) -> bool:
    """True when the topic has an active reply-button mute (state["muted"]).

    Expired or malformed entries never suppress, and any error fails open (no
    suppression), so a mute can never silence by accident.
    """
    try:
        return control.until_active((state.get("muted") or {}).get(topic))
    except Exception:  # noqa: BLE001 - mute enforcement must never drop on error
        return False


def _apply_mute(state: dict, event: Event, action: str) -> str:
    """Downgrade a routed action while the event's topic is muted.

        push   -> digest   the live ring stops, but the item lands in the next
                           morning digest (defer, don't drop — nothing is lost)
        digest -> drop     the chatter the mute was aimed at

    ``critical`` severity is exempt: a mute aimed at chatty news must never
    silence a real alert (storm warning, outage), so those still ring through.
    This is what makes the "Mute movies 24h" button do what it says — before
    this, only digest-bound items were muted and the noisy live pushes (e.g. a
    trailer-leak news storm scoring "high") kept firing through the mute.
    """
    if action not in ("push", "digest") or event.severity == "critical":
        return action
    if not _mute_active(state, event.topic):
        return action
    if action == "push":
        log.info("mute active for %r: deferring %r to the morning digest",
                 event.topic, event.title)
        return "digest"
    log.info("mute active for %r: dropping digest-bound %r",
             event.topic, event.title)
    return "drop"


def _follow_upgrades(state: dict, event: Event, action: str) -> bool:
    """True when an active FOLLOW should turn this digest-bound item into a
    live push (at default priority).

    Mirror of _apply_mute, with three deliberate asymmetries: only "digest"
    upgrades (a "drop" stays dropped — the follow amplifies the middle band,
    it doesn't resurrect what the engine judged noise), an active MUTE wins
    over a follow, and any error fails open to "no boost".
    """
    if action != "digest":
        return False
    try:
        if _mute_active(state, event.topic):
            return False
        if not control.until_active(
                (state.get(control.FOLLOWED_KEY) or {}).get(event.topic)):
            return False
    except Exception:  # noqa: BLE001 - follow must never break routing
        return False
    log.info("follow active for %r: pushing %r instead of digesting",
             event.topic, event.title)
    return True


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
            # The body lets body-informative topics (holidays, reminders, fx)
            # keep their detail when digested; collector/news items have no body
            # and render title-only as before.
            "detail": event.body,
            "preserve_detail": bool(event.metadata.get("preserve_detail")),
            # Lets the digest flush offer a [Follow <hot topic>] button for
            # the topic contributing the day's top item (docs/design/05).
            "topic": event.topic,
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
    change: "Optional[Change]" = None,
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
    ``change`` (a ``changes.Change``) is the opt-in change-summary hook: when given,
    it fills an empty ``body`` with ``change.summary`` (the human "how it moved" line)
    and stashes the STRUCTURED move under ``metadata["change"]`` so the digest detail,
    the ntfy body, and the event log all read the same data with no sentence re-parsing
    (see docs/design/01-change-summary-framework.md). Omitting it is byte-identical to
    before, so the framework is pull, not push.

    ``priority_cfg`` / ``digest_cfg`` default to the monitors.json sections and
    exist mainly so tests can inject synthetic config.
    """
    md = dict(metadata or {})
    if click_url is not None:
        md.setdefault("click_url", click_url)
    if tags is not None:
        md.setdefault("tags", tags)
    if change is not None:
        if not body:
            body = change.summary
        md.setdefault("change", {**change.metadata, "summary": change.summary,
                                 "kind": change.kind, "direction": change.direction})

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
        # Engine OFF -> reproduce the caller's exact pre-engine behavior (plus
        # the same overnight deferral the engine path gets, so quiet hours are
        # equally safe to enable in legacy mode).
        action = legacy_action
        if action == "push" and _quiet_defers(legacy_priority):
            log.info("quiet hours: deferring %r to the morning digest", title)
            action = "digest"
        action = _apply_mute(state, event, action)
        if _follow_upgrades(state, event, action):
            _push(event, "default")
            action = "push"
        elif action == "digest":
            _to_digest(state, event, score, digest_cfg)
        elif action != "drop":
            _push(event, legacy_priority)
        # Log under the caller's within-domain score so history is complete even
        # when the engine is off (the dashboard reads this regardless of mode).
        eventlog.record(state, event, action, score, priority_cfg)
        return state

    action = decision.action
    if action == "push" and _quiet_defers(decision.ntfy_priority):
        log.info("quiet hours: deferring %r to the morning digest", title)
        action = "digest"
    action = _apply_mute(state, event, action)
    if _follow_upgrades(state, event, action):
        _push(event, "default")
        action = "push"
    elif action == "push":
        _push(event, decision.ntfy_priority)
    elif action == "digest":
        _to_digest(state, event, decision.score, digest_cfg)
    # "drop" -> intentionally nothing
    # Record every routed Event (push/digest/drop) with its global score so the
    # dashboard has a durable, cross-topic history that outlives the digest flush.
    eventlog.record(state, event, action, decision.score, priority_cfg)
    return state
