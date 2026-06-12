"""Topic: one consolidated daily learning push.

Bundles up to three short sections into a SINGLE daily notification:
  - On this day  - a historical event for today's date (Wikimedia featured feed)
  - Featured     - Wikipedia's featured article of the day (title + extract)
  - A curated fact - one vetted entry from a rotating knowledge-base channel
                     (science / technology / life skills / general knowledge /
                     the structured "Knowledge" deep-dive channel)

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
import random

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
    ("Dominican culture", "dr_culture.json"),
    ("Money basics", "personal_finance.json"),
    ("Word of the Day", "vocabulary.json"),  # structured entries; never LLM-reworded
    ("Knowledge", "knowledge.json"),  # structured entries; never LLM-reworded
]

# --- "Knowledge" channel ----------------------------------------------------
# Unlike the plain {text, src} channels above, knowledge.json entries are
# structured ({id, category, title, body, tags}) and the pick has memory: the
# id of each shown entry is stamped into state.json so nothing repeats within
# KNOWLEDGE_REPEAT_DAYS, and a category pointer advances cyclically so
# consecutive picks never cluster on one theme. The in-category pick is a
# date-seeded RNG: random across days, identical on a same-day re-run (the
# same re-run safety the day-of-year channels get for free).
KNOWLEDGE_LABEL = "Knowledge"
KNOWLEDGE_FILE = "knowledge.json"
KNOWLEDGE_SEEN_KEY = "knowledge_seen"  # {entry_id: "YYYY-MM-DD" last shown}
KNOWLEDGE_CATEGORY_KEY = "knowledge_last_category"
KNOWLEDGE_REPEAT_DAYS = 30

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


def _format_vocab_entry(entry: dict) -> str:
    """Render a vocabulary.json entry into a multiline push-notification body."""
    word = entry.get("word", "")
    pronunciation = entry.get("pronunciation", "")
    pos = entry.get("pos", "")
    definition = entry.get("definition", "") or entry.get("text", "")
    example = entry.get("example", "")
    src = entry.get("src", "")
    parts = [word]
    meta = " · ".join(p for p in [pronunciation, pos] if p)
    if meta:
        parts.append(meta)
    if definition:
        parts.append(definition)
    if example:
        parts.append(f'"{example}"')
    if src:
        parts.append(f"(Source: {src})")
    return "\n".join(p for p in parts if p)


def _wotd_fact(day: _dt.date | None = None) -> tuple[str, str]:
    """('Word of the Day', body) from the local vocabulary KB; ('', '') if none.

    Local-only: the vetted KB plus the day-of-year pick keeps the push
    deterministic and free of an extra feed dependency. Entries are formatted
    structurally, never LLM-reworded — a dictionary definition stays verbatim.
    """
    items = kb.load(kb.DATA_DIR / "vocabulary.json", field="word")
    chosen = kb.pick(items, day=day)
    if not chosen:
        return "", ""
    return "Word of the Day", _format_vocab_entry(chosen)


def _knowledge_entries() -> list[dict]:
    """knowledge.json entries that carry everything the push needs."""
    items = kb.load(kb.DATA_DIR / KNOWLEDGE_FILE, field="body")
    return [e for e in items if e.get("id") and e.get("category") and e.get("title")]


def _knowledge_recent(state: dict, day: _dt.date) -> dict[str, _dt.date]:
    """{entry_id: last-shown date} for ids still inside the no-repeat window."""
    recent: dict[str, _dt.date] = {}
    for entry_id, stamp in (state.get(KNOWLEDGE_SEEN_KEY) or {}).items():
        try:
            seen = _dt.date.fromisoformat(str(stamp))
        except ValueError:
            continue  # malformed stamp: treat as never seen
        if 0 <= (day - seen).days < KNOWLEDGE_REPEAT_DAYS:
            recent[str(entry_id)] = seen
    return recent


def _knowledge_fact(state: dict, day: _dt.date | None = None) -> tuple[str, str]:
    """(title, body) for today's knowledge entry; records the pick in state.

    Rotates to the next category (sorted, cyclic after the one stamped in
    state) that still has an entry unseen within KNOWLEDGE_REPEAT_DAYS, then
    picks one of its eligible entries with a date-seeded RNG. If every entry
    in the KB was shown recently (only possible while the KB is small), the
    least recently shown one is reused so the section never goes silent.
    """
    day = day or _dt.date.today()
    entries = _knowledge_entries()
    if not entries:
        return "", ""

    recent = _knowledge_recent(state, day)
    categories = sorted({str(e["category"]) for e in entries})
    last = str(state.get(KNOWLEDGE_CATEGORY_KEY, ""))
    start = (categories.index(last) + 1) % len(categories) if last in categories else 0
    rotation = categories[start:] + categories[:start]

    rng = random.Random(day.isoformat())
    chosen: dict | None = None
    for category in rotation:
        eligible = [e for e in entries
                    if str(e["category"]) == category and str(e["id"]) not in recent]
        if eligible:
            chosen = rng.choice(eligible)
            break
    if chosen is None:
        chosen = min(entries, key=lambda e: recent.get(str(e["id"]), _dt.date.min))

    # Rebuilding the seen-map from `recent` also prunes stamps that expired.
    seen = {entry_id: shown.isoformat() for entry_id, shown in recent.items()}
    seen[str(chosen["id"])] = day.isoformat()
    state[KNOWLEDGE_SEEN_KEY] = seen
    state[KNOWLEDGE_CATEGORY_KEY] = str(chosen["category"])
    return str(chosen["title"]), str(chosen["body"])


def _curated_fact(day: _dt.date | None = None,
                  state: dict | None = None) -> tuple[str, str]:
    """(label, fact_text) from today's rotating KB channel; ('', '') if none."""
    label, filename = CHANNELS[kb.day_of_year(day) % len(CHANNELS)]
    if label == "Word of the Day":
        return _wotd_fact(day)
    if label == KNOWLEDGE_LABEL:
        # Header is the entry's own title; body verbatim (never LLM-reworded).
        return _knowledge_fact(state if state is not None else {}, day)
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


def _wiki_image_url(feed: dict) -> str | None:
    """Return the Wikipedia picture-of-the-day URL from the feed, or None."""
    try:
        image = feed.get("image")
        if not isinstance(image, dict):
            return None
        for key in ("thumbnail", "image"):
            src = (image.get(key) or {}).get("source")
            if src:
                return str(src)
    except Exception:  # noqa: BLE001 - missing image must never break the push
        pass
    return None


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

    label, fact = _curated_fact(today, state)
    if fact:
        sections.append((label, fact))

    if not sections:
        log.warning("learning push has nothing to send today; skipping")
        return state

    image_url = _wiki_image_url(feed)
    events.emit(
        state,
        title="Daily learning",
        body=_compose(sections),
        topic="learn",
        severity="low",
        source="Learning",
        click_url=click_url,
        tags="books",
        metadata={"attach_url": image_url} if image_url else None,
        legacy_priority="low",
        legacy_action="push",
    )
    log.info("sent daily learning push (%d section(s))", len(sections))
    state[STATE_KEY] = _today()
    return state
