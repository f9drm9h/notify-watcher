"""Topic: one calm, educational "Today's spark" push per day about electricity.

This is the teaching counterpart to the news-alerting ``energy`` topic: instead of
"industry news alert" it sends a single, unhurried daily item that gradually builds
understanding of electricity, grids, generation, nuclear, storage, transmission,
renewables, reliability, history, and infrastructure (see
docs/design/03-energy-learning-topic.md).

How it picks what to teach:
  * CURATED (default): four content channels — History / Facts / Modern / Infrastructure
    — rotate by day-of-year; within the day's channel an UNSEEN entry is chosen (tracked
    in state) so nothing repeats until that channel is exhausted, then its seen-list
    resets for a fresh pass. Content is local JSON, delivered VERBATIM (an LLM never
    rewords a curated fact), so it works fully offline and is deterministic on re-run.
  * NEWS (occasional): if a fresh, high-enough energy story exists in the shared
    ``event_log`` (the news ``energy`` topic records every routed item there) and enough
    days have passed since the last news slot, that day shows the top story instead, with
    a one-line "why it matters" via ``summarize`` (graceful headline fallback, no key
    required).

Daily-only (``NOTIFY_DAILY``) and idempotent (``energy_learn_last_sent == today``), so a
repeated or rebased runner invocation never double-sends — matching learn/health_tip.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os

from .. import config, events, kb, summarize

log = logging.getLogger(__name__)

LAST_SENT_KEY = "energy_learn_last_sent"
SEEN_KEY = "energy_learn_seen"          # {channel_key: [delivered id, ...]}
LAST_NEWS_KEY = "energy_learn_last_news"
EVENT_LOG_KEY = "event_log"             # shared sink; see notify_watcher.eventlog

# (channel key, display label, KB filename). The day-of-year selects the channel;
# an unseen-first pick selects the entry within it. Add content by editing JSON.
CHANNELS: list[tuple[str, str, str]] = [
    ("history", "Historical event", "energy_history.json"),
    ("facts", "How it works", "energy_facts.json"),
    ("modern", "Modern development", "energy_modern.json"),
    ("infrastructure", "Infrastructure spotlight", "energy_infrastructure.json"),
]

_DEFAULT_MIN_NEWS_SCORE = 6
_DEFAULT_NEWS_GAP_DAYS = 5
_NEWS_WITHIN_HOURS = 24

_WHY_SYSTEM = (
    "You explain why an energy or electricity news headline matters, for a curious "
    "non-expert. Given only the headline, reply with ONE plain-text sentence of at "
    "most ~30 words stating its practical significance. No preamble, no markdown, no "
    "quotation marks. Do not invent specifics that the headline does not imply."
)


def _today() -> str:
    return _dt.date.today().isoformat()


def _parse_ts(s) -> _dt.datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        return _dt.datetime.fromisoformat(s)
    except ValueError:
        return None


def _top_recent_energy(event_log: list, now: _dt.datetime) -> dict | None:
    """Highest-scoring ``energy`` event_log entry within the last 24h, or None."""
    cutoff = now - _dt.timedelta(hours=_NEWS_WITHIN_HOURS)
    best: dict | None = None
    for e in event_log:
        if not isinstance(e, dict) or e.get("topic") != "energy":
            continue
        ts = _parse_ts(e.get("ts"))
        if ts is None or ts < cutoff:
            continue
        if best is None or int(e.get("score", 0) or 0) > int(best.get("score", 0) or 0):
            best = e
    return best


def _should_use_news(state: dict, story: dict | None, today: _dt.date, cfg: dict) -> bool:
    """News day iff a story clears the score bar AND enough days since the last one."""
    if not story:
        return False
    if int(story.get("score", 0) or 0) < int(cfg.get("min_news_score", _DEFAULT_MIN_NEWS_SCORE)):
        return False
    last = _parse_date(state.get(LAST_NEWS_KEY))
    if last is not None:
        gap = int(cfg.get("news_min_gap_days", _DEFAULT_NEWS_GAP_DAYS))
        if (today - last).days < gap:
            return False
    return True


def _parse_date(s) -> _dt.date | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        return _dt.date.fromisoformat(s)
    except ValueError:
        return None


def _curated_channel(day: _dt.date) -> tuple[str, str, list[dict]]:
    """The day's channel (key, label, entries). Falls through to the next non-empty
    channel if a file is missing/empty, so a content gap never blocks the day."""
    n = len(CHANNELS)
    start = kb.day_of_year(day) % n
    for i in range(n):
        ckey, label, filename = CHANNELS[(start + i) % n]
        items = kb.load(kb.DATA_DIR / filename, field="what")
        if items:
            return ckey, label, items
    return "", "", []


def _pick_unseen(items: list[dict], seen: list, day: _dt.date) -> tuple[dict | None, list]:
    """Pick an entry whose id isn't in ``seen`` (deterministic per day); reset when the
    channel is exhausted. Returns (entry, updated_seen)."""
    seen_set = set(seen)
    unseen = [it for it in items if it.get("id") not in seen_set]
    if not unseen:                      # full pass complete -> start fresh
        seen = []
        unseen = items
    entry = kb.pick(unseen, day=day)
    if entry is not None:
        seen = seen + [entry.get("id")]
    # cap the seen-list to the channel size so it can't grow unbounded
    return entry, seen[-len(items):]


def _compose_curated(entry: dict) -> str:
    parts = [
        f"What: {entry.get('what', '')}",
        f"Why it matters: {entry.get('why', '')}",
        f"Why you should care: {entry.get('care', '')}",
    ]
    body = "\n".join(parts)
    if entry.get("src"):
        body += f"\n(Source: {entry['src']})"
    return body


def _compose_news(story: dict) -> tuple[str, str | None]:
    """(body, click_url) for the occasional news slot, with a headline-only fallback."""
    headline = story.get("title", "")
    lines = [f'Headline: "{headline}"']
    why = summarize.one_line(_WHY_SYSTEM, headline)
    if why:
        lines.append(f"Why it matters: {why}")
    if story.get("source"):
        lines.append(f"({story['source']} · tap to read)")
    return "\n".join(lines), (story.get("url") or None)


def run(state: dict) -> dict:
    if not os.environ.get("NOTIFY_DAILY"):
        return state  # only the daily cron run sends the learning push
    if state.get(LAST_SENT_KEY) == _today():
        log.info("energy learning push already sent today; skipping")
        return state

    cfg = config.section("energy_learn")
    today = _dt.date.today()
    now = _dt.datetime.now(_dt.timezone.utc)

    story = _top_recent_energy(state.get(EVENT_LOG_KEY) or [], now)
    if _should_use_news(state, story, today, cfg):
        body, click_url = _compose_news(story)
        events.emit(
            state,
            title="⚡ Energy now",
            body=body,
            topic="energy_learn",
            severity="low",
            source="Energy learning",
            click_url=click_url,
            tags="bulb",
            legacy_priority="low",
            legacy_action="push",
        )
        state[LAST_NEWS_KEY] = _today()
        log.info("energy learning: sent news slot (score %s)", story.get("score"))
        state[LAST_SENT_KEY] = _today()
        return state

    ckey, label, items = _curated_channel(today)
    if not items:
        log.warning("energy learning: no curated content available; skipping today")
        return state  # no stamp -> retry on the next daily run

    seen_map: dict = state.setdefault(SEEN_KEY, {})
    entry, updated_seen = _pick_unseen(items, list(seen_map.get(ckey) or []), today)
    if entry is None:
        log.warning("energy learning: channel %r yielded no entry; skipping", ckey)
        return state

    events.emit(
        state,
        title=f"⚡ Today's spark — {entry.get('title') or label}",
        body=_compose_curated(entry),
        topic="energy_learn",
        severity="low",
        source="Energy learning",
        tags="bulb",
        legacy_priority="low",
        legacy_action="push",
    )
    seen_map[ckey] = updated_seen
    log.info("energy learning: sent curated %s item %r", ckey, entry.get("id"))
    state[LAST_SENT_KEY] = _today()
    return state
