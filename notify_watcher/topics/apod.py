"""Topic: NASA Astronomy Picture of the Day — the first *visual* notification.

Fetches NASA's APOD (free API; the shared anonymous DEMO_KEY allows 50
requests/day per IP and this topic makes one per day, so no key is required —
set a free NASA_API_KEY from api.nasa.gov to get your own quota) and pushes the
day's picture with a short caption. The image rides the ntfy ``Attach`` header
(see ntfy.push), so it renders inline in the notification; tapping opens the
full-resolution version.

Video days (APOD is occasionally a video) attach the video's thumbnail when
NASA provides one and link to the video itself. Daily-only (NOTIFY_DAILY) and
deduped per APOD date, so a duplicate or drifted run never double-sends and a
failed fetch retries on the next 3-hourly run of the same day.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os

import requests

from .. import events

log = logging.getLogger(__name__)

STATE_KEY = "apod_last_sent"
API_URL = "https://api.nasa.gov/planetary/apod"
HEADERS = {"User-Agent": "notify-watcher/1.0 (+https://github.com/) personal-use"}
APOD_HOME = "https://apod.nasa.gov/apod/astropix.html"
_MAX_CAPTION = 280


def _fetch() -> dict:
    key = os.environ.get("NASA_API_KEY", "").strip() or "DEMO_KEY"
    resp = requests.get(
        API_URL,
        params={"api_key": key, "thumbs": "true"},
        headers=HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}


def _truncate(text: str, limit: int = _MAX_CAPTION) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(".,;:") + "..."


def _compose(data: dict) -> tuple[str, str, str | None, str | None] | None:
    """Pure. (title, body, attach_url, click_url) from an APOD payload, or None.

    Image days attach the standard-res image (right size for a notification)
    and click through to the HD version. Video days attach NASA's thumbnail
    when present and click through to the video. No title or no usable URL ->
    None (nothing worth sending).
    """
    title = str(data.get("title") or "").strip()
    url = str(data.get("url") or "").strip()
    if not title or not url:
        return None
    media = data.get("media_type")
    if media == "image":
        attach: str | None = url
        click = str(data.get("hdurl") or "").strip() or url
    elif media == "video":
        attach = str(data.get("thumbnail_url") or "").strip() or None
        click = url
    else:
        return None
    body = _truncate(str(data.get("explanation") or ""))
    copyright_ = " ".join(str(data.get("copyright") or "").split())
    if copyright_:
        body = f"{body}\n(c) {copyright_}" if body else f"(c) {copyright_}"
    return f"APOD: {title}", body, attach, click


def run(state: dict) -> dict:
    if not os.environ.get("NOTIFY_DAILY"):
        return state  # one picture per day, on the daily run
    try:
        data = _fetch()
    except Exception as exc:  # noqa: BLE001 - fetch failure is non-fatal
        log.warning("APOD fetch failed: %s", exc)
        return state

    # Dedup on NASA's own date when present (a payload without one falls back
    # to today's date, so a degraded API still can't double-send within a day).
    apod_date = str(data.get("date") or "").strip() or _dt.date.today().isoformat()
    if state.get(STATE_KEY) == apod_date:
        log.info("APOD for %s already sent; skipping", apod_date)
        return state

    composed = _compose(data)
    if composed is None:
        log.warning("APOD payload had nothing sendable; skipping")
        return state
    title, body, attach, click = composed

    state = events.emit(
        state,
        title=title,
        body=body,
        topic="apod",
        severity="low",
        source="NASA APOD",
        click_url=click or APOD_HOME,
        tags="milky_way",
        metadata={"attach_url": attach} if attach else None,
        legacy_priority="low",
        legacy_action="push",
    )
    state[STATE_KEY] = apod_date
    log.info("sent APOD for %s", apod_date)
    return state
