"""Topic: one consolidated daily learning push.

Bundles up to three short sections into a SINGLE daily notification:
  - On this day  - a historical event for today's date (Wikimedia featured feed)
  - Featured     - Wikipedia's featured article of the day (title + extract)
  - A curated fact - one vetted entry from a rotating knowledge-base channel
                     (science / technology / life skills / general knowledge)

Design choices that match the rest of the project:
  * Free / no key. The Wikimedia REST feed needs no auth; the KB channels are
    local JSON.
  * Deterministic. The feed is editorially fixed per date and the KB pick is a
    day-of-year rotation (see notify_watcher.kb), so a re-run on the same date
    produces the same push - safe against the runner's repeated/rebased runs.
  * Consolidated to ONE push so a steady learning drip never causes fatigue.
  * Graceful degradation. Each section is independent: if Wikimedia is
    unreachable we still send the curated fact, and vice versa. Only when there
    is nothing at all to say do we skip.
  * LLM optional. The curated fact may be reworded for variety via
    notify_watcher.summarize, falling back to the verbatim vetted text.

Daily-only (NOTIFY_DAILY) and guarded by learn_last_sent so a duplicate or
drifted run never double-sends.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os

import requests

from .. import events, kb, summarize

log = logging.getLogger(__name__)

STATE_KEY = "learn_last_sent"

FEED_URL = "https://en.wikipedia.org/api/rest_v1/feed/featured/{y}/{m:02d}/{d:02d}"
HEADERS = {
    "User-Agent": "notify-watcher/1.0 (personal daily learning digest; +https://github.com/)"
}
_MAX_EXTRACT = 280  # keep the featured blurb to a couple of sentences

# Rotating curated channels: (display label, KB file). The day-of-year selects
# the channel; kb.pick selects the entry within it, so both rotate over time.
CHANNELS: list[tuple[str, str]] = [
    ("Science", "science_facts.json"),
    ("Technology", "tech_literacy.json"),
    ("Life skill", "life_skills.json"),
    ("Did you know", "general_knowledge.json"),
]

_REWORD_SYSTEM = (
    "You reword a single educational fact for a daily push notification. "
    "Preserve the meaning and any numbers, names, and dates EXACTLY; do not add "
    "new claims or facts. Reply with one plain-text sentence of at most ~35 "
    "words: no preamble, no markdown, no quotation marks."
)


def _today() -> str:
    return _dt.date.today().isoformat()


def _fetch_feed(day: _dt.date) -> dict:
    """Fetch the Wikimedia 'featured content' feed for a date, or {} on failure."""
    url = FEED_URL.format(y=day.year, m=day.month, d=day.day)
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}


def _truncate(text: str, limit: int = _MAX_EXTRACT) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(".,;:") + "..."


def _on_this_day_line(feed: dict, day: _dt.date | None = None) -> str:
    """One 'On this day' event for the date, chosen deterministically, or ''."""
    events = [e for e in (feed.get("onthisday") or [])
              if isinstance(e, dict) and e.get("text")]
    chosen = kb.pick(events, day=day)
    if not chosen:
        return ""
    year = chosen.get("year")
    text = " ".join(str(chosen.get("text", "")).split())
    return f"{year}: {text}" if year else text


def _featured(feed: dict) -> tuple[str, str, str | None]:
    """(title, extract, page_url) for the featured article; ('', '', None) if none."""
    tfa = feed.get("tfa")
    if not isinstance(tfa, dict):
        return "", "", None
    title = tfa.get("normalizedtitle") or tfa.get("title") or ""
    title = title.replace("_", " ")
    extract = _truncate(tfa.get("extract", ""))
    url = (((tfa.get("content_urls") or {}).get("desktop") or {}).get("page"))
    return title, extract, url


def _curated_fact(day: _dt.date | None = None) -> tuple[str, str]:
    """(label, fact_text) from today's rotating KB channel; ('', '') if none."""
    label, filename = CHANNELS[kb.day_of_year(day) % len(CHANNELS)]
    items = kb.load(kb.DATA_DIR / filename)
    chosen = kb.pick(items, day=day)
    if not chosen:
        return "", ""
    text = str(chosen.get("text", ""))
    src = chosen.get("src", "")
    reworded = summarize.one_line(_REWORD_SYSTEM, text) or text
    if src:
        reworded = f"{reworded} (Source: {src})"
    return label, reworded


def _compose(sections: list[tuple[str, str]]) -> str:
    """Render [(header, body), ...] into a scannable multi-section message."""
    blocks = [f"{header}\n{body}" for header, body in sections if body]
    return "\n\n".join(blocks)


def run(state: dict) -> dict:
    if not os.environ.get("NOTIFY_DAILY"):
        return state  # only the daily cron run sends the learning push
    if state.get(STATE_KEY) == _today():
        log.info("learning push already sent today; skipping")
        return state

    today = _dt.date.today()
    try:
        feed = _fetch_feed(today)
    except Exception as exc:  # noqa: BLE001 - feed failure is non-fatal
        log.warning("Wikimedia feed fetch failed: %s", exc)
        feed = {}

    sections: list[tuple[str, str]] = []
    click_url: str | None = None

    otd = _on_this_day_line(feed, today)
    if otd:
        sections.append(("On this day", otd))

    title, extract, url = _featured(feed)
    if title:
        sections.append((f"Featured: {title}", extract))
        click_url = url

    label, fact = _curated_fact(today)
    if fact:
        sections.append((label, fact))

    if not sections:
        log.warning("learning push has nothing to send today; skipping")
        return state

    events.emit(
        state,
        title="Daily learning",
        body=_compose(sections),
        topic="learn",
        severity="low",
        source="Learning",
        click_url=click_url,
        tags="books",
        legacy_priority="low",
        legacy_action="push",
    )
    log.info("sent daily learning push (%d section(s))", len(sections))
    state[STATE_KEY] = _today()
    return state
