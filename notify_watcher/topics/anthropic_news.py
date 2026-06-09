"""Topic: official Anthropic announcements (Google News, free, no key).

The user wants the things Anthropic itself publishes - model releases (Opus
4.8 -> 4.9), Claude Code updates, usage/policy notes - not the wider press. We
query Google News for Anthropic + Claude and keep only entries whose <source> is
Anthropic itself, which reliably surfaces their own posts ("Introducing Claude
Opus 4.8 - Anthropic") while dropping third-party coverage. Dedup by article id;
the first run seeds silently. (X-only posts aren't on this feed.)
"""
from __future__ import annotations

import logging
import urllib.parse

import feedparser
import requests

from .. import config, events, ids, news

log = logging.getLogger(__name__)

STATE_KEY = "anthropic_seen"
CAP = 200
QUERY = "Anthropic Claude (Opus OR Sonnet OR Haiku OR model OR release OR update)"
RSS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; notify-watcher/1.0)"}
OFFICIAL_SOURCE = "anthropic"


def _entry_source(entry) -> str:
    src = getattr(entry, "source", None)
    if isinstance(src, dict):
        return (src.get("title") or "").strip()
    return (getattr(src, "title", "") or "").strip()


def _official(entries, max_age_days: float = 0) -> list[tuple]:
    """Pure: keep only entries published by Anthropic itself, and — when
    ``max_age_days`` > 0 — published within that window. Google News resurfaces
    years-old posts under brand-new URLs (a 2023 "introducing Claude Pro" pushed
    in 2026), which defeats id dedup; the age gate is what stops those.
    Returns [(id, title, link)]."""
    out: list[tuple] = []
    for e in entries:
        if _entry_source(e).lower() != OFFICIAL_SOURCE:
            continue
        if not news.is_recent(e, max_age_days):
            continue
        aid = getattr(e, "id", "") or getattr(e, "link", "")
        if aid:
            out.append((aid, getattr(e, "title", ""), getattr(e, "link", "")))
    return out


def run(state: dict) -> dict:
    # Allow disabling/retuning via config presence, but no required fields.
    config.section("anthropic")
    url = RSS.format(q=urllib.parse.quote_plus(QUERY))
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        entries = feedparser.parse(resp.content).entries
    except Exception as exc:  # noqa: BLE001 - non-fatal
        log.error("anthropic news fetch failed: %s", exc)
        return state

    max_age = config.section("news").get("max_age_days", news.DEFAULT_MAX_AGE_DAYS)
    official = _official(entries, max_age)
    log.info("anthropic: %d entries, %d official+recent", len(entries), len(official))

    seen = state.get(STATE_KEY)
    if seen is None:
        state[STATE_KEY] = [ids.short(a[0]) for a in official][:CAP]
        log.info("seeded %s baseline with %d id(s) (no alerts on first run)",
                 STATE_KEY, len(state[STATE_KEY]))
        return state

    seen = ids.normalize_seen(seen)
    seen_set = set(seen)
    fresh: list[str] = []
    pushed = 0
    for aid, title, link in official:
        h = ids.short(aid)
        if h in seen_set:
            continue
        seen_set.add(h)
        fresh.append(h)
        state = events.emit(
            state,
            title="Anthropic",
            body=title or "New post from Anthropic",
            topic="anthropic_news",
            severity="moderate",
            source="Anthropic",
            click_url=link or None,
            tags="robot",
            legacy_priority="default",
            legacy_action="push",
        )
        pushed += 1

    if pushed:
        log.info("anthropic: %d new post(s)", pushed)
    state[STATE_KEY] = (fresh + seen)[:CAP]
    return state
