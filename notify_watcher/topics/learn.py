"""Topic: one consolidated daily learning push.

Bundles up to three short sections into a SINGLE daily notification:
  - On this day  - a historical event for today's date (Wikimedia featured feed)
  - Featured     - Wikipedia's featured article of the day (title + extract)
  - A curated fact - one vetted entry from a rotating knowledge-base channel
                     (science / technology / life skills / general knowledge)

Also owns the standalone "Knowledge" push: a rich, multi-paragraph story
generated on demand by Gemini on EVERY watcher run (every 3 hours), independent
of the daily gate. The topic is chosen from data/knowledge_topics.json (500+
topics across ten categories) using category rotation + a 30-day no-repeat
window; the narrative itself is written fresh each time by summarize.brief, so
no bodies are stored. If Gemini is unavailable the push is skipped cleanly and
retried next run. Guarded by a per-3-hour-window stamp so a re-run inside the
same window never double-sends; see _run_knowledge.

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

The consolidated push is daily-only (NOTIFY_DAILY) and guarded by
learn_last_sent so a duplicate or drifted run never double-sends.
"""
from __future__ import annotations

import datetime as _dt
import json
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
]

# --- "Knowledge" push (every run, not part of the daily rotation) -----------
# A Gemini-powered story engine. knowledge_topics.json maps each category to a
# list of topic prompts (no bodies); the pick has memory — the id (category:topic)
# of each shown topic is stamped into state.json so nothing repeats within
# KNOWLEDGE_REPEAT_DAYS, and a category pointer advances cyclically so consecutive
# picks never cluster on one theme. On every watcher run (the every-3-hours cron,
# independent of NOTIFY_DAILY) one topic is chosen and narrated fresh by Gemini.
# The in-category pick is seeded with the current 3-hour window (date + hour-of-day
# bucket): each window picks its own topic, while a re-run inside the same window
# re-picks the same one. The chosen topic is recorded (and KNOWLEDGE_SENT_KEY
# stamps the window) ONLY after Gemini returns a story, so a generation failure
# leaves the window unstamped and the topic unconsumed and simply retries next run.
KNOWLEDGE_LABEL = "Knowledge"
KNOWLEDGE_FILE = "knowledge_topics.json"
KNOWLEDGE_SEEN_KEY = "knowledge_seen"  # {topic_id: "YYYY-MM-DD" last shown}
KNOWLEDGE_CATEGORY_KEY = "knowledge_last_category"
KNOWLEDGE_SENT_KEY = "knowledge_last_sent"  # "YYYY-MM-DDTn" 3-hour window
KNOWLEDGE_REPEAT_DAYS = 30
KNOWLEDGE_WINDOW_HOURS = 3  # the watcher cron's cadence

# Story generation budget. ~1024 output tokens comfortably covers four
# substantial paragraphs; the result is clipped to KNOWLEDGE_CLIP_CHARS so a
# long narrative stays under ntfy's ~4 KB message limit (clip on a sentence
# boundary so it never ends mid-word).
KNOWLEDGE_STORY_TOKENS = 1024
KNOWLEDGE_CLIP_CHARS = 3500

_STORY_SYSTEM = (
    "You are a documentary narrator writing for an intelligent, curious adult. "
    "Write rich, narrative, storytelling prose — never bullet points and never a "
    "dry encyclopedia entry. Cover who was involved, what happened, why it "
    "matters, what came before and after, and any compelling human drama. Write "
    "at least four substantial paragraphs. Plain text only: no markdown, no "
    "headings, no lists."
)

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
    """Flatten knowledge_topics.json into [{id, category, title}] topic entries.

    The file maps each category to a list of topic prompt strings; there are no
    stored bodies (Gemini writes the story per push). The id is a stable
    ``category:title`` slug so the 30-day seen-map keeps working across runs. A
    missing or malformed file yields [] (logged), so a bad KB never crashes the
    run — the caller treats empty as "nothing to send".
    """
    path = kb.DATA_DIR / KNOWLEDGE_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("could not load knowledge topics %s: %s", KNOWLEDGE_FILE, exc)
        return []
    categories = data.get("categories") if isinstance(data, dict) else None
    if not isinstance(categories, dict):
        log.error("knowledge topics %s has no 'categories' map", KNOWLEDGE_FILE)
        return []
    entries: list[dict] = []
    for category, topics in categories.items():
        if not isinstance(topics, list):
            continue
        for topic in topics:
            if isinstance(topic, str) and topic.strip():
                title = topic.strip()
                entries.append({"id": f"{category}:{title}",
                                "category": str(category), "title": title})
    return entries


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


def _knowledge_window(now: _dt.datetime) -> str:
    """The 3-hour-window stamp for a datetime, e.g. '2026-06-16T4'.

    Bucketing the hour (instead of using it raw) keeps the stamp stable when
    a cron run drifts a few minutes inside its window, so the seed and the
    double-send guard agree about which window a run belongs to.
    """
    return f"{now.date().isoformat()}T{now.hour // KNOWLEDGE_WINDOW_HOURS}"


def _knowledge_pick(state: dict, now: _dt.datetime | None = None) -> dict | None:
    """The topic entry to narrate this run, or None when the KB is empty.

    Pure selection — does NOT mutate state. Rotates to the next category
    (sorted, cyclic after the one stamped in state) that still has a topic
    unseen within KNOWLEDGE_REPEAT_DAYS, then picks one of its eligible topics
    with an RNG seeded by the current 3-hour window — each window picks its own
    topic, a re-run inside the window re-picks the same one. If every topic was
    shown recently, the least recently shown one is reused so the push is never
    starved. Recording the pick is the caller's job (_knowledge_commit), done
    only after a story is successfully generated.
    """
    now = now or _dt.datetime.now()
    entries = _knowledge_entries()
    if not entries:
        return None

    recent = _knowledge_recent(state, now.date())
    categories = sorted({str(e["category"]) for e in entries})
    last = str(state.get(KNOWLEDGE_CATEGORY_KEY, ""))
    start = (categories.index(last) + 1) % len(categories) if last in categories else 0
    rotation = categories[start:] + categories[:start]

    rng = random.Random(_knowledge_window(now))
    for category in rotation:
        eligible = [e for e in entries
                    if str(e["category"]) == category and str(e["id"]) not in recent]
        if eligible:
            return rng.choice(eligible)
    return min(entries, key=lambda e: recent.get(str(e["id"]), _dt.date.min))


def _knowledge_commit(state: dict, chosen: dict, day: _dt.date) -> None:
    """Record `chosen` as shown today and advance the category pointer.

    Rebuilding the seen-map from `recent` also prunes stamps that expired.
    """
    recent = _knowledge_recent(state, day)
    seen = {topic_id: shown.isoformat() for topic_id, shown in recent.items()}
    seen[str(chosen["id"])] = day.isoformat()
    state[KNOWLEDGE_SEEN_KEY] = seen
    state[KNOWLEDGE_CATEGORY_KEY] = str(chosen["category"])


def _clip_story(text: str, limit: int = KNOWLEDGE_CLIP_CHARS) -> str:
    """Trim a story to `limit` chars on a sentence boundary (never mid-word)."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    if cut > limit // 2:
        return head[:cut + 1]
    return head.rsplit(" ", 1)[0].rstrip() + "…"


def _generate_story(topic: str) -> str | None:
    """A rich, multi-paragraph narrative for `topic` via Gemini, or None.

    Delegates to summarize.brief (Gemini first, Anthropic fallback, both
    optional), which never raises and returns None when no provider key is set
    or every call fails — so a flaky/absent LLM skips the push cleanly rather
    than crashing the sweep. The result is clipped to fit ntfy's message limit.
    """
    prompt = (
        f"Write a rich, narrative, storytelling account of {topic}. Cover who "
        "was involved, what happened, why it matters, what came before and "
        "after, and any compelling human drama. Write at least four substantial "
        "paragraphs. Do not use bullet points. Write as a documentary narrator "
        "for an intelligent, curious adult."
    )
    story = summarize.brief(_STORY_SYSTEM, prompt, max_tokens=KNOWLEDGE_STORY_TOKENS)
    return _clip_story(story) if story else None


def _run_knowledge(state: dict, now: _dt.datetime | None = None) -> dict:
    """Send one Gemini-narrated knowledge story per 3-hour window; every cycle.

    Picks the topic, then asks Gemini for the narrative. The topic is recorded
    and the window stamped ONLY after a story comes back, so a generation
    failure leaves both untouched and the next run retries (never crashing the
    sweep). The window stamp in KNOWLEDGE_SENT_KEY makes a re-run inside the
    same window a no-op.
    """
    now = now or _dt.datetime.now()
    window = _knowledge_window(now)
    if state.get(KNOWLEDGE_SENT_KEY) == window:
        log.info("knowledge push already sent this window; skipping")
        return state

    chosen = _knowledge_pick(state, now)
    if chosen is None:
        log.warning("knowledge KB has no topics to send; skipping")
        return state

    story = _generate_story(chosen["title"])
    if not story:
        log.warning("knowledge story generation failed for %r; retrying next run",
                    chosen["title"])
        return state

    _knowledge_commit(state, chosen, now.date())
    events.emit(
        state,
        title=chosen["title"],
        body=story,
        topic="learn",
        severity="low",
        source="Learning",
        tags="bulb",
        legacy_priority="low",
        legacy_action="push",
    )
    log.info("sent knowledge story %r for window %s", chosen["title"], window)
    state[KNOWLEDGE_SENT_KEY] = window
    return state


def _curated_fact(day: _dt.date | None = None) -> tuple[str, str]:
    """(label, fact_text) from today's rotating KB channel; ('', '') if none."""
    label, filename = CHANNELS[kb.day_of_year(day) % len(CHANNELS)]
    if label == "Word of the Day":
        return _wotd_fact(day)
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
    state = _run_knowledge(state)  # every run, independent of the daily gate
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
