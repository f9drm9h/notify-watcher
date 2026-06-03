"""Topic: WWDC announcements from Apple Newsroom RSS.

We pull the official RSS feed (free), keep items whose title matches WWDC, and
push any URL we have not pushed before. The notification body is a one-line AI
summary when a provider key (GEMINI_API_KEY, then ANTHROPIC_API_KEY) is set,
and falls back to the headline otherwise — see _ai_summary(). The summary is
isolated inside build_notification(), so the fetch/dedup/push plumbing never
changes regardless of which path produced the body.
"""
from __future__ import annotations

import logging

import feedparser

from .. import ntfy, summarize

log = logging.getLogger(__name__)

FEED_URL = "https://www.apple.com/newsroom/rss-feed.rss"
STATE_KEY = "wwdc_seen_urls"
# Match the acronym AND the spelled-out name: Apple often titles articles
# "Worldwide Developers Conference" without the "WWDC" acronym, and those
# would otherwise be silently missed.
KEYWORDS = ("wwdc", "worldwide developers conference")
MAX_REMEMBERED = 200  # cap state size; the feed only carries recent items


def _title_matches(title: str) -> bool:
    t = title.lower()
    return any(k in t for k in KEYWORDS)

# --- Optional AI summary ----------------------------------------------------
# The notification body becomes a one-line AI summary of the article when a
# provider key is set; otherwise it falls back to the headline. The provider
# plumbing (Gemini → Anthropic, both optional) lives in notify_watcher.summarize.
# Doubles as a "final answer only" instruction so models that reason by default
# don't emit a preamble.
_SUMMARY_SYSTEM = (
    "You write one-line push-notification summaries of Apple WWDC news items. "
    "Given a headline and an optional article blurb, reply with a single "
    "plain-text sentence of at most ~30 words describing what was announced. "
    "Output only that sentence: no preamble, no markdown, no quotation marks."
)


def _ai_summary(entry) -> str | None:
    """Return a one-line AI summary, or None to fall back to the headline."""
    title = getattr(entry, "title", "")
    blurb = (getattr(entry, "summary", "") or "")[:2000]  # bound input tokens
    return summarize.one_line(
        _SUMMARY_SYSTEM, f"Headline: {title}\n\nArticle blurb:\n{blurb}"
    )


def build_notification(entry) -> tuple[str, str, str]:
    """Return (title, body, click_url) for a feed entry.

    `body` is a one-line AI summary when a provider key is set and the call
    succeeds, otherwise the headline. The (title, body, click_url) contract is
    identical in both cases, so the fetch/dedup/push code in run() is
    unaffected by which path produced the body.
    """
    title = "Apple WWDC: " + getattr(entry, "title", "(no title)")
    link = getattr(entry, "link", "")
    body = _ai_summary(entry) or getattr(entry, "title", "")
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
        if _title_matches(getattr(e, "title", ""))
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
