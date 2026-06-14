"""Outbound notification delivery + quiet-hours policy.

Historically this module POSTed to ntfy.sh. The transport is now Discord (see
``discord_delivery``): ``push`` renders a rich embed and delivers it to the
topic's routed channel. The module name and the ``push``/``would_suppress``
surface are kept deliberately — every topic calls ``ntfy.push`` through the
shared module object and the test suite patches it as the single delivery seam,
so keeping the seam stable made the transport swap a drop-in.

What stayed: the entire quiet-hours engine. Whether the underlying transport is
ntfy or Discord, "don't ring my phone at 3am for low-priority chatter" is the
same routing decision, so ``_quiet_suppresses`` / ``would_suppress`` are
unchanged and still consulted by ``events.emit`` before it routes.

Configuration now lives in ``discord_delivery`` (DISCORD_TOKEN + the CHANNEL_*
channel ids). Nothing secret is hardcoded here, so this file is safe to commit.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
from typing import Optional

from . import config, discord_delivery

log = logging.getLogger(__name__)

# Priorities that always ring through, even during quiet hours: real, timely
# threats and explicitly high-priority announcements are sent at these tiers.
# The priority engine is responsible for deciding when a non-safety topic, such
# as an official Anthropic model release, belongs here.
_ALWAYS_DELIVER = {"high", "urgent"}

# Back-compat alias: external references to the old config error keep importing.
DiscordConfigError = discord_delivery.DiscordConfigError


def _parse_hhmm(value: object) -> Optional[int]:
    """Minutes-since-midnight for a 'HH:MM' string, or None if unparseable."""
    if not isinstance(value, str):
        return None
    parts = value.split(":")
    if len(parts) != 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if 0 <= h < 24 and 0 <= m < 60:
        return h * 60 + m
    return None


def _in_window(now_minutes: int, start: int, end: int) -> bool:
    """True if now is within [start, end), handling a window that wraps midnight."""
    if start == end:
        return False  # zero-width window suppresses nothing
    if start < end:
        return start <= now_minutes < end
    return now_minutes >= start or now_minutes < end  # wraps past midnight


def _quiet_suppresses(priority: Optional[str], cfg: dict, now_utc: _dt.datetime) -> bool:
    """Pure decision: should a push at this priority be held for quiet hours?

    Suppresses only when quiet hours are explicitly enabled, the (local) time is
    inside the window, and the priority is not a high/urgent alert. Any missing
    or malformed config yields False (fail open), so a typo never silences pushes.
    """
    if not cfg.get("enabled"):
        return False
    if (priority or "") in _ALWAYS_DELIVER:
        return False
    start = _parse_hhmm(cfg.get("start"))
    end = _parse_hhmm(cfg.get("end"))
    if start is None or end is None:
        return False
    offset = cfg.get("utc_offset_hours", -4)  # DR is UTC-4 year-round (no DST)
    if not isinstance(offset, (int, float)):
        return False
    local = now_utc + _dt.timedelta(hours=offset)
    return _in_window(local.hour * 60 + local.minute, start, end)


def _is_quiet_now(priority: Optional[str]) -> bool:
    """Wrap the quiet-hours decision so any failure fails OPEN (sends the push)."""
    if os.environ.get("NOTIFY_TEST_PUSH"):
        return False  # the delivery self-test must never be suppressed
    try:
        cfg = config.section("quiet_hours")
        return _quiet_suppresses(priority, cfg, _dt.datetime.now(_dt.timezone.utc))
    except Exception:  # noqa: BLE001 - suppression must never drop a push on error
        return False


def would_suppress(priority: Optional[str]) -> bool:
    """Public quiet-hours probe: would a push at this priority be held right now?

    Lets the routing layer (events.emit) ask BEFORE calling push, so it can
    defer a would-be-suppressed notification into the daily digest instead of
    letting the transport drop it on the floor. Same fail-open semantics as the
    internal check."""
    return _is_quiet_now(priority)


def push(
    title: str,
    message: str,
    click_url: Optional[str] = None,
    tags: Optional[str] = None,
    priority: Optional[str] = None,
    attach_url: Optional[str] = None,
    actions: Optional[list] = None,
    timeout: float = 15.0,
    topic: Optional[str] = None,
    severity: Optional[str] = None,
) -> None:
    """Deliver a notification as a Discord rich embed to the topic's channel.

    `topic` drives routing (see discord_delivery.category_for): finance topics
    land in CHANNEL_FINANCE, discovery in CHANNEL_DISCOVERY, system/errors in
    CHANNEL_LOGS, the Gemini summaries in CHANNEL_BRIEFING, and anything
    unmapped in CHANNEL_GENERAL. `severity` tints the embed (critical -> red).

    `priority` is no longer a transport header — it is retained because the
    quiet-hours engine bands on it ("high"/"urgent" always ring; lower tiers can
    be held overnight). `click_url`, `tags`, and `attach_url` are folded into the
    embed (link, emoji cue, inline image).

    `actions` (the old ntfy reply buttons) is accepted but not delivered: those
    were ntfy http-action headers. The Discord equivalent is interactive message
    components handled by the gateway bot (bot.py) and is intentionally out of
    scope for this transport swap — see the migration note in the PR/commit.

    Raises discord_delivery.DiscordConfigError when unconfigured and
    requests.HTTPError on a non-2xx Discord response, so callers that gate on a
    successful send (e.g. the digest buffer clear) behave as before.

    When quiet hours are enabled (monitors.json -> quiet_hours) and active, a
    low/default/min push is dropped silently; high/urgent alerts always ring.
    """
    if _is_quiet_now(priority):
        log.info("quiet hours active; suppressing %r push", title)
        return

    if actions:
        log.debug("discord transport ignores %d ntfy action button(s) for %r",
                  len(actions), title)

    discord_delivery.send(
        topic,
        title,
        message,
        click_url=click_url,
        tags=tags,
        severity=severity,
        attach_url=attach_url,
        timeout=timeout,
    )
