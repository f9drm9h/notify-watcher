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
import os

import feedparser

from .. import ntfy

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

# --- Optional Claude-powered summary ---------------------------------------
# When ANTHROPIC_API_KEY is set, the notification body becomes a one-line
# Claude summary of the article; otherwise we fall back to the headline. Set
# the key locally or as a GitHub Actions secret to turn this on.
SUMMARY_MODEL = "claude-opus-4-8"
# Stable across every entry, so it goes in `system` with a cache breakpoint.
# (The per-entry headline/blurb is volatile and goes in the user turn, after
# the breakpoint.) Doubles as the "final answer only" instruction that keeps
# Opus 4.8 from emitting reasoning preamble when thinking is off.
_SUMMARY_SYSTEM = (
    "You write one-line push-notification summaries of Apple WWDC news items. "
    "Given a headline and an optional article blurb, reply with a single "
    "plain-text sentence of at most ~30 words describing what was announced. "
    "Output only that sentence: no preamble, no markdown, no quotation marks."
)


def _ai_summary(entry) -> str | None:
    """Return a Claude-generated one-line summary, or None to fall back.

    Never raises. Returns None — meaning "use the headline" — whenever a
    summary can't be produced: ANTHROPIC_API_KEY is unset, the anthropic SDK
    isn't installed, or the API call fails/returns empty. Keeping this total
    means one flaky API call never silences a real WWDC alert.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        log.info("anthropic SDK not installed; using headline-only body")
        return None

    title = getattr(entry, "title", "")
    blurb = (getattr(entry, "summary", "") or "")[:2000]  # bound input tokens

    try:
        # Short timeout + single retry so a hung call falls back fast rather
        # than stalling the scheduled run (SDK default timeout is 10 minutes).
        client = anthropic.Anthropic(max_retries=1)
        resp = client.with_options(timeout=15.0).messages.create(
            model=SUMMARY_MODEL,
            max_tokens=256,
            system=[
                {
                    "type": "text",
                    "text": _SUMMARY_SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": f"Headline: {title}\n\nArticle blurb:\n{blurb}",
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001 - any failure → headline fallback
        log.warning("AI summary failed (%s); using headline-only body", exc)
        return None

    text = next((b.text for b in resp.content if b.type == "text"), "").strip()
    return text or None


def build_notification(entry) -> tuple[str, str, str]:
    """Return (title, body, click_url) for a feed entry.

    `body` is a Claude one-line summary when ANTHROPIC_API_KEY is set and the
    call succeeds, otherwise the headline. The (title, body, click_url)
    contract is identical in both cases, so the fetch/dedup/push code in
    run() is unaffected by which path produced the body.
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
