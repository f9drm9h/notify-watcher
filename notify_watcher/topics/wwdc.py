"""Topic: WWDC announcements from Apple Newsroom RSS.

Headline-only — no external AI, no paid API. We pull the official RSS feed
(free), keep items whose title contains "WWDC", and push any URL we have
not pushed before.

The notification body is built by build_notification() so a future version
can swap the headline for an AI-generated summary without touching the
fetch/dedup/push plumbing.
"""
from __future__ import annotations

import logging

import feedparser

from .. import ntfy

log = logging.getLogger(__name__)

FEED_URL = "https://www.apple.com/newsroom/rss-feed.rss"
STATE_KEY = "wwdc_seen_urls"
KEYWORD = "WWDC"
MAX_REMEMBERED = 200  # cap state size; the feed only carries recent items


def build_notification(entry) -> tuple[str, str, str]:
    """Return (title, body, click_url) for a feed entry.

    TODO(ai-summary): swap `body` for an AI-generated summary of
    entry.summary / fetched article body. Keep the (title, body, click_url)
    contract so the rest of this module does not change.
    """
    title = "Apple WWDC: " + getattr(entry, "title", "(no title)")
    body = getattr(entry, "title", "")  # headline-only for now
    link = getattr(entry, "link", "")
    return title, body, link


def _entry_id(entry) -> str:
    """Stable identifier for dedup: prefer the article URL, fall back to id."""
    return getattr(entry, "link", "") or getattr(entry, "id", "")


def run(state: dict) -> dict:
    feed = feedparser.parse(FEED_URL)
    if getattr(feed, "bozo", 0) and not feed.entries:
        raise RuntimeError(f"feed parse failed: {getattr(feed, 'bozo_exception', '')}")

    seen: list[str] = list(state.get(STATE_KEY, []))
    seen_set = set(seen)

    matched = [
        e for e in feed.entries
        if KEYWORD.lower() in getattr(e, "title", "").lower()
    ]
    log.info("feed entries: %d, WWDC matches: %d", len(feed.entries), len(matched))

    pushed = 0
    for entry in matched:
        eid = _entry_id(entry)
        if not eid or eid in seen_set:
            continue
        title, body, link = build_notification(entry)
        ntfy.push(title=title, message=body, click_url=link, tags="apple")
        seen.append(eid)
        seen_set.add(eid)
        pushed += 1

    if pushed:
        log.info("pushed %d new WWDC item(s)", pushed)

    # Keep only the most recent N IDs so state.json stays small.
    state[STATE_KEY] = seen[-MAX_REMEMBERED:]
    return state
