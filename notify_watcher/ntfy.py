"""Send a push notification via ntfy.sh.

Reads NTFY_TOPIC (required) and NTFY_SERVER (optional, defaults to
https://ntfy.sh) from environment variables. Nothing is hardcoded so this
file is safe to commit to a public repo.
"""
from __future__ import annotations

import base64
import datetime as _dt
import logging
import os
from typing import Optional

import requests

from . import config

log = logging.getLogger(__name__)

DEFAULT_SERVER = "https://ntfy.sh"

# Priorities that always ring through, even during quiet hours: real, timely
# threats (earthquakes, hurricanes, tsunami advisories) are sent at these tiers.
_ALWAYS_DELIVER = {"high", "urgent"}


class NtfyConfigError(RuntimeError):
    pass


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


def _encode_header(value: str) -> str:
    """Make a header value safe for ntfy without mangling non-ASCII text.

    HTTP/ntfy header values must be ASCII (requests encodes them latin-1). A pure
    ASCII title passes through unchanged. Anything with accents/emoji is wrapped
    as a single RFC 2047 base64 encoded-word (``=?UTF-8?B?...?=``), which ntfy
    decodes back to UTF-8 — so "Café" renders as "Café" instead of the "CafÃ©"
    you got from the old utf-8-bytes-as-latin-1 reinterpretation.
    """
    try:
        value.encode("ascii")
        return value
    except UnicodeEncodeError:
        b64 = base64.b64encode(value.encode("utf-8")).decode("ascii")
        return f"=?UTF-8?B?{b64}?="


def _config() -> tuple[str, str]:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        raise NtfyConfigError(
            "NTFY_TOPIC environment variable is not set. "
            "Set it to your private ntfy topic name."
        )
    server = os.environ.get("NTFY_SERVER", "").strip() or DEFAULT_SERVER
    return server.rstrip("/"), topic


def push(
    title: str,
    message: str,
    click_url: Optional[str] = None,
    tags: Optional[str] = None,
    priority: Optional[str] = None,
    attach_url: Optional[str] = None,
    timeout: float = 15.0,
) -> None:
    """POST a notification to the configured ntfy topic.

    `priority` is an optional ntfy priority name ("min", "low", "default",
    "high", "urgent"); when None the server applies its default, so existing
    callers are unaffected. Used by the scored domain monitors to make
    breakthrough/high-tier alerts ring louder than routine ones.

    `attach_url` sets the ntfy `Attach` header: the app fetches the URL and,
    for images, renders the picture inline in the notification. The server
    only stores the link (not the file), so any size works on the free tier.

    Raises requests.HTTPError on a non-2xx response so callers can decide
    whether to retry or log-and-continue.

    When quiet hours are enabled (monitors.json -> quiet_hours) and active, a
    low/default/min push is dropped silently; high/urgent alerts always ring.
    """
    if _is_quiet_now(priority):
        log.info("quiet hours active; suppressing %r push", title)
        return

    server, topic = _config()
    url = f"{server}/{topic}"

    headers: dict[str, str] = {}
    # ntfy header values must be ASCII; RFC 2047-encode non-ASCII titles so
    # accented characters survive instead of being mojibake'd through latin-1.
    headers["Title"] = _encode_header(title)
    if click_url:
        headers["Click"] = click_url
    if tags:
        headers["Tags"] = tags
    if priority:
        headers["Priority"] = priority
    if attach_url:
        headers["Attach"] = attach_url

    resp = requests.post(
        url,
        data=message.encode("utf-8"),
        headers=headers,
        timeout=timeout,
    )
    resp.raise_for_status()
