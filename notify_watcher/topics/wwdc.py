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

# --- Optional AI summary ----------------------------------------------------
# The notification body becomes a one-line AI summary of the article when a
# provider key is set; otherwise it falls back to the headline. Providers are
# tried in order of preference: Gemini (free tier) first, then Anthropic.
# Set GEMINI_API_KEY and/or ANTHROPIC_API_KEY as GitHub Actions secrets.
GEMINI_MODEL = "gemini-2.5-flash"
ANTHROPIC_MODEL = "claude-opus-4-8"
# Doubles as a "final answer only" instruction so models that reason by default
# don't emit a preamble.
_SUMMARY_SYSTEM = (
    "You write one-line push-notification summaries of Apple WWDC news items. "
    "Given a headline and an optional article blurb, reply with a single "
    "plain-text sentence of at most ~30 words describing what was announced. "
    "Output only that sentence: no preamble, no markdown, no quotation marks."
)


def _entry_text(entry) -> tuple[str, str]:
    """Extract (headline, bounded blurb) used as the model input."""
    title = getattr(entry, "title", "")
    blurb = (getattr(entry, "summary", "") or "")[:2000]  # bound input tokens
    return title, blurb


def _summary_gemini(title: str, blurb: str) -> str | None:
    """One-line summary via the free Gemini API (plain REST, no SDK).

    Returns None on any failure so the caller falls back to the headline.
    """
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return None
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
    )
    payload = {
        "system_instruction": {"parts": [{"text": _SUMMARY_SYSTEM}]},
        "contents": [
            {"parts": [{"text": f"Headline: {title}\n\nArticle blurb:\n{blurb}"}]}
        ],
        # Disable "thinking" so the small output budget isn't spent on reasoning.
        "generationConfig": {"maxOutputTokens": 256, "thinkingConfig": {"thinkingBudget": 0}},
    }
    try:
        resp = requests.post(
            url, params={"key": key}, json=payload, timeout=15.0
        )
        resp.raise_for_status()
        cands = resp.json().get("candidates") or []
        parts = (cands[0].get("content", {}).get("parts") if cands else None) or []
        text = "".join(p.get("text", "") for p in parts).strip()
        return text or None
    except Exception as exc:  # noqa: BLE001 - any failure → headline fallback
        log.warning("Gemini summary failed (%s); trying next provider", exc)
        return None


def _summary_anthropic(title: str, blurb: str) -> str | None:
    """One-line summary via Claude. Returns None on any failure."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        log.info("anthropic SDK not installed; skipping Claude summary")
        return None
    try:
        # Short timeout + single retry so a hung call falls back fast rather
        # than stalling the scheduled run (SDK default timeout is 10 minutes).
        client = anthropic.Anthropic(max_retries=1)
        resp = client.with_options(timeout=15.0).messages.create(
            model=ANTHROPIC_MODEL,
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
        log.warning("Claude summary failed (%s); using headline-only body", exc)
        return None
    return next((b.text for b in resp.content if b.type == "text"), "").strip() or None


def _ai_summary(entry) -> str | None:
    """Return a one-line AI summary, or None to fall back to the headline.

    Never raises. Tries each provider in preference order and returns the first
    non-empty result; if no provider key is set or every call fails, returns
    None so a flaky/absent API never silences a real WWDC alert.
    """
    title, blurb = _entry_text(entry)
    for provider in (_summary_gemini, _summary_anthropic):
        summary = provider(title, blurb)
        if summary:
            return summary
    return None


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
