"""Topic: a Gemini-narrated Library of Congress historical item story.

A standalone educational push (not gated on NOTIFY_DAILY). Each watcher cycle
picks one focus area, fetches public historical items from the Library of
Congress JSON API, prefers an item with a usable image, and asks Gemini (via
summarize.brief) to write a documentary-style story about the artifact and its
world. If the LOC fetch fails, parsing yields no item, or Gemini returns None,
the run skips cleanly without stamping the 3-hour window or consuming the item.
"""
from __future__ import annotations

import datetime as _dt
import logging
import random
from collections.abc import Iterable
from typing import Any

import requests

from .. import events, summarize

log = logging.getLogger(__name__)

TOPIC = "library_of_congress"
API_URL = "https://www.loc.gov/search/"
HEADERS = {
    "User-Agent": "notify-watcher/1.0 (personal Library of Congress stories; +https://github.com/)"
}

FOCUS_AREAS: tuple[str, ...] = (
    "American history milestones",
    "World War photographs",
    "Civil rights movement",
    "Presidential documents",
    "Early 20th century life",
    "Historic maps and exploration",
    "Scientific and invention records",
)

LOC_SEEN_KEY = "library_of_congress_seen"          # {item_id: "YYYY-MM-DD" last shown}
LOC_FOCUS_KEY = "library_of_congress_last_focus"
LOC_SENT_KEY = "library_of_congress_last_sent"     # "YYYY-MM-DDTn" 3-hour window
LOC_REPEAT_DAYS = 30
LOC_WINDOW_HOURS = 3

LOC_STORY_TOKENS = 1024
LOC_CLIP_CHARS = 3500

_STORY_SYSTEM = (
    "You are a history documentary narrator writing for an intelligent, curious "
    "adult. Write rich, narrative prose. Plain text only: no markdown, no "
    "headings, no lists."
)


def _window(now: _dt.datetime) -> str:
    """The 3-hour-window stamp for a datetime, e.g. '2026-06-16T4'."""
    return f"{now.date().isoformat()}T{now.hour // LOC_WINDOW_HOURS}"


def _recent(state: dict, day: _dt.date) -> dict[str, _dt.date]:
    """{item_id: last-shown date} for ids still inside the no-repeat window."""
    recent: dict[str, _dt.date] = {}
    for item_id, stamp in (state.get(LOC_SEEN_KEY) or {}).items():
        try:
            seen = _dt.date.fromisoformat(str(stamp))
        except ValueError:
            continue
        if 0 <= (day - seen).days < LOC_REPEAT_DAYS:
            recent[str(item_id)] = seen
    return recent


def _next_focus(state: dict) -> str:
    """Rotate evenly through focus areas using the last successful focus stamp."""
    last = str(state.get(LOC_FOCUS_KEY, ""))
    start = (FOCUS_AREAS.index(last) + 1) % len(FOCUS_AREAS) if last in FOCUS_AREAS else 0
    return FOCUS_AREAS[start]


def _first_text(value: Any) -> str:
    """First non-empty string from a scalar/list-ish LOC field."""
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, Iterable) and not isinstance(value, (dict, bytes, bytearray)):
        for item in value:
            text = _first_text(item)
            if text:
                return text
    return ""


def _first_url(value: Any) -> str | None:
    """First usable http(s) URL from a scalar/list-ish LOC field."""
    text = _first_text(value)
    return text if text.startswith(("https://", "http://")) else None


def _date_text(raw: dict) -> str:
    """Best human date/period from the varied LOC result shape."""
    item = raw.get("item") if isinstance(raw.get("item"), dict) else {}
    for key in ("date_display", "date", "created_published_date"):
        text = _first_text(raw.get(key))
        if text:
            return text
    for key in ("date", "created_published", "created"):
        text = _first_text(item.get(key))
        if text:
            return text
    return "an unknown date"


def _item_id(raw: dict) -> str:
    """Stable LOC item identity, preferring canonical id/link fields."""
    for key in ("id", "url", "link"):
        text = _first_text(raw.get(key))
        if text:
            return text
    return ""


def _normalize_item(raw: dict, focus: str) -> dict | None:
    """Normalize one LOC API result into the fields this topic needs."""
    if not isinstance(raw, dict):
        return None
    title = _first_text(raw.get("title"))
    item_id = _item_id(raw)
    if not title or not item_id:
        return None
    url = _first_url(raw.get("url")) or _first_url(raw.get("link")) or _first_url(raw.get("id"))
    return {
        "id": item_id,
        "title": title,
        "date": _date_text(raw),
        "description": _first_text(raw.get("description")) or _first_text(raw.get("subject")),
        "url": url,
        "image_url": _first_url(raw.get("image_url")),
        "focus": focus,
    }


def _fetch_items(focus: str) -> list[dict]:
    """Fetch and normalize LOC search results for one focus area.

    Raises on transport/HTTP errors so run() can skip without consuming state.
    """
    params = {"fo": "json", "q": focus, "c": 25}
    resp = requests.get(API_URL, params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    results = data.get("results") if isinstance(data, dict) else []
    if not isinstance(results, list):
        return []
    return [item for raw in results
            if (item := _normalize_item(raw, focus)) is not None]


def _pick_item(state: dict, focus: str, items: list[dict], now: _dt.datetime) -> dict | None:
    """Pick one item, preferring image-bearing unseen items in this focus area."""
    if not items:
        return None
    recent = _recent(state, now.date())
    eligible = [item for item in items if str(item["id"]) not in recent]
    pool = eligible or items
    with_images = [item for item in pool if item.get("image_url")]
    pool = with_images or pool
    rng = random.Random(f"{_window(now)}:{focus}")
    return rng.choice(pool)


def _commit(state: dict, chosen: dict, focus: str, day: _dt.date) -> None:
    """Record `chosen` as shown today and advance the focus pointer."""
    recent = _recent(state, day)
    seen = {item_id: shown.isoformat() for item_id, shown in recent.items()}
    seen[str(chosen["id"])] = day.isoformat()
    state[LOC_SEEN_KEY] = seen
    state[LOC_FOCUS_KEY] = focus


def _clip_story(text: str, limit: int = LOC_CLIP_CHARS) -> str:
    """Trim a story to `limit` chars on a sentence boundary when possible."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    if cut > limit // 2:
        return head[:cut + 1]
    return head.rsplit(" ", 1)[0].rstrip() + "..."


def _generate_story(item: dict) -> str | None:
    """A rich LOC historical-item story via Gemini, or None on provider failure."""
    prompt = (
        "This is a historical item from the Library of Congress titled "
        f"'{item['title']}' from approximately {item['date']}. Write a rich "
        "narrative account of: what this item is, who created it or appears in "
        "it, what was happening in the world at this moment, why it is "
        "historically significant, and what came after. Write at least 4 "
        "substantial paragraphs as a history documentary narrator for an "
        "intelligent curious adult. Do not use bullet points."
    )
    story = summarize.brief(_STORY_SYSTEM, prompt, max_tokens=LOC_STORY_TOKENS)
    return _clip_story(story) if story else None


def _run(state: dict, now: _dt.datetime) -> dict:
    """Implementation with injectable time for deterministic tests."""
    window = _window(now)
    if state.get(LOC_SENT_KEY) == window:
        log.info("Library of Congress story already sent this window; skipping")
        return state

    focus = _next_focus(state)
    try:
        items = _fetch_items(focus)
    except Exception as exc:  # noqa: BLE001 - source failure is a clean skip
        log.warning("Library of Congress fetch failed for %r: %s", focus, exc)
        return state

    chosen = _pick_item(state, focus, items, now)
    if chosen is None:
        log.warning("Library of Congress returned no usable items for %r", focus)
        return state

    story = _generate_story(chosen)
    if not story:
        log.warning("Library of Congress story generation failed for %r; retrying next run",
                    chosen["title"])
        return state

    metadata = {}
    if chosen.get("url"):
        metadata["click_url"] = chosen["url"]
    if chosen.get("image_url"):
        metadata["attach_url"] = chosen["image_url"]

    events.emit(
        state,
        title=f"Library of Congress: {chosen['title']}",
        body=story,
        topic=TOPIC,
        severity="low",
        source="Library of Congress",
        tags="classical_building",
        metadata=metadata,
        legacy_priority="low",
        legacy_action="push",
    )
    _commit(state, chosen, focus, now.date())
    state[LOC_SENT_KEY] = window
    log.info("sent Library of Congress story for %r (window %s)", chosen["title"], window)
    return state


def run(state: dict) -> dict:
    """Send one LOC historical-item story per 3-hour window; every cycle."""
    return _run(state, _dt.datetime.now())
