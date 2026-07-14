"""The "quote" content provider for the consolidated spark topic.

No longer a standalone TOPICS entry: topics/spark.py owns the 6-hour firing
gate and the quote -> fact -> health-tip rotation, and calls ``send_quote``
when the rotation lands on a quote. Each send picks one historical figure from
a curated category list, fetches a REAL quote of theirs from Wikiquote (the
free, no-key MediaWiki ``action=parse`` API), and asks Gemini (via
summarize.brief) to write a short 2-3 paragraph narrative about who they were,
the context of the quote, and why it still resonates. The quote is genuine;
only the surrounding narrative is generated.

Design choices mirror the "Knowledge" story engine in topics/learn.py so the two
behave identically and share the same operational guarantees:

  * Free / no key. Wikiquote's REST/action API needs no auth; the figure list is
    a local Python constant (FIGURES).
  * Even category rotation. A category pointer in state advances cyclically so
    consecutive pushes never cluster on one theme.
  * 30-day no-repeat. Each shown figure's id (``category:name``) is stamped into
    state.json so no figure repeats within WIKIQUOTE_REPEAT_DAYS.
  * Window-seeded determinism. The in-category figure pick and the quote pick are
    seeded by the current 6-hour window (date + hour bucket, matching spark's
    cadence), so a re-run inside the same window re-picks the same figure and
    quote — safe against the runner's repeated/rebased runs — while each new
    window serves something new. (The double-send guard itself lives in spark.)
  * Graceful skip. If the quote fetch fails, parsing yields nothing, or Gemini
    returns None, the send returns False cleanly WITHOUT consuming the figure —
    spark leaves its window unstamped and the next run simply retries. A
    per-topic failure never crashes the sweep.
  * LLM optional. summarize.brief tries Gemini first, then Anthropic, and returns
    None when no provider key is set — so an absent/flaky LLM skips the push
    rather than sending a bare quote.
  * Health contract. spark is in health.ADOPTED: every send that contacts
    Wikiquote reports source_ok / source_failed under the spark topic, so a
    source that silently starts failing for weeks (how the retired gutenberg
    topic died) surfaces in topic_health and the watchdog instead of hiding
    behind the graceful skip. An LLM failure after a successful fetch still
    reports source_failed — a dead LLM key must not look healthy forever.
"""
from __future__ import annotations

import datetime as _dt
import logging
import random
import re

import requests

from .. import events, health, summarize

log = logging.getLogger(__name__)

# Everything emits (and reports health) under the consolidated spark topic;
# spark.py owns the TOPICS entry, this module is just its quote provider.
TOPIC = "spark"

# --- Wikiquote source -------------------------------------------------------
API_URL = "https://en.wikiquote.org/w/api.php"
HEADERS = {
    "User-Agent": "notify-watcher/1.0 (personal daily quotation digest; +https://github.com/)"
}

# A quote candidate is sane only within this length band: shorter is usually a
# stray fragment, longer is usually a multi-sentence passage that reads poorly
# as a push header.
_MIN_QUOTE_CHARS = 25
_MAX_QUOTE_CHARS = 320
_MIN_QUOTE_WORDS = 5

# --- Curated figures, grouped into categories for even rotation -------------
# Keys are stable category ids (renaming one resets its 30-day dedup memory).
# Each value is a list of figures whose English Wikiquote page title equals the
# name (the action API follows redirects, so minor variants still resolve); a
# page that yields no parseable quote is simply skipped that run.
FIGURES: dict[str, list[str]] = {
    "science_pioneers": [
        "Albert Einstein", "Isaac Newton", "Marie Curie", "Charles Darwin",
        "Galileo Galilei", "Richard Feynman", "Carl Sagan", "Nikola Tesla",
        "Stephen Hawking", "Ada Lovelace",
    ],
    "philosophers": [
        "Socrates", "Plato", "Aristotle", "Confucius", "Friedrich Nietzsche",
        "Immanuel Kant", "Seneca the Younger", "Marcus Aurelius", "Voltaire",
        "Arthur Schopenhauer",
    ],
    "writers": [
        "William Shakespeare", "Mark Twain", "Jane Austen", "Leo Tolstoy",
        "Oscar Wilde", "Virginia Woolf", "Ernest Hemingway",
        "Fyodor Dostoevsky", "Maya Angelou", "George Orwell",
    ],
    "leaders": [
        "Abraham Lincoln", "Winston Churchill", "Nelson Mandela",
        "Mahatma Gandhi", "Theodore Roosevelt", "Thomas Jefferson",
        "Eleanor Roosevelt", "Benjamin Franklin", "Frederick Douglass",
        "Mikhail Gorbachev",
    ],
    "innovators_artists": [
        "Leonardo da Vinci", "Vincent van Gogh", "Pablo Picasso", "Steve Jobs",
        "Walt Disney", "Frida Kahlo", "Ludwig van Beethoven", "Thomas Edison",
        "Henry Ford", "Buckminster Fuller",
    ],
    "activists_thinkers": [
        "Martin Luther King, Jr.", "Helen Keller", "Malala Yousafzai",
        "Rosa Parks", "Susan B. Anthony", "Mother Teresa", "Albert Schweitzer",
        "Booker T. Washington", "Henry David Thoreau", "Ralph Waldo Emerson",
    ],
}

# --- State + windowing ------------------------------------------------------
WIKIQUOTE_SEEN_KEY = "wikiquote_seen"            # {figure_id: "YYYY-MM-DD" last shown}
WIKIQUOTE_CATEGORY_KEY = "wikiquote_last_category"
WIKIQUOTE_REPEAT_DAYS = 30
WIKIQUOTE_WINDOW_HOURS = 6  # spark's cadence; seeds the per-window picks

# Story generation budget. ~512 output tokens comfortably covers 2-3 short
# paragraphs; the result is clipped to WIKIQUOTE_CLIP_CHARS so the narrative
# plus the quote stays a short read (and far under Discord's embed limit).
WIKIQUOTE_STORY_TOKENS = 512
WIKIQUOTE_CLIP_CHARS = 1600

_STORY_SYSTEM = (
    "You are a documentary narrator writing for an intelligent, curious adult. "
    "Write rich, narrative, storytelling prose — never bullet points and never a "
    "dry encyclopedia entry. Write 2-3 short paragraphs, no more. Plain "
    "text only: no markdown, no headings, no lists, no surrounding quotation marks."
)


# --- Wikiquote fetch + parse ------------------------------------------------
def _fetch_wikitext(page: str) -> str:
    """Raw wikitext of a Wikiquote page via the no-key action API. Raises on HTTP error."""
    params = {
        "action": "parse",
        "page": page,
        "prop": "wikitext",
        "format": "json",
        "formatversion": "2",
        "redirects": "1",
    }
    resp = requests.get(API_URL, params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return str(((data.get("parse") or {}).get("wikitext")) or "")


# Section headings whose bullets are NOT the subject's own words (quotes ABOUT
# them, disputed/misattributed lines, link lists). We stop collecting inside any
# heading whose title contains one of these tokens.
_SKIP_SECTION_TOKENS = (
    "about", "external", "see also", "references", "notes", "disputed",
    "misattributed", "quotes about", "sources",
)

_HEADING_RE = re.compile(r"^\s*={2,}\s*(.*?)\s*={2,}\s*$")
# A top-level quote bullet: a single leading "*" with content (not "**" sources,
# not "*:" continuations).
_QUOTE_BULLET_RE = re.compile(r"^\*(?![*:])\s*(.+)$")


def _clean_markup(text: str) -> str:
    """Reduce a wikitext line to plain prose, dropping refs/templates/markup."""
    text = re.sub(r"<ref[^>]*?/>", "", text)
    text = re.sub(r"<ref[^>]*?>.*?</ref>", "", text, flags=re.DOTALL)
    # Templates can nest; strip innermost {{...}} repeatedly until none remain.
    while "{{" in text:
        new = re.sub(r"\{\{[^{}]*\}\}", "", text)
        if new == text:
            break
        text = new
    text = re.sub(r"<[^>]+>", "", text)                       # remaining HTML tags
    text = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", text)   # [[a|b]] -> b
    text = re.sub(r"\[\[([^\]]*)\]\]", r"\1", text)            # [[a]]   -> a
    text = re.sub(r"\[(?:https?|ftp)://\S+\s+([^\]]*)\]", r"\1", text)  # [url text] -> text
    text = re.sub(r"\[(?:https?|ftp)://\S+\]", "", text)       # bare [url] -> ''
    text = text.replace("'''''", "").replace("'''", "").replace("''", "")
    text = text.strip().strip('"“”').strip()
    return re.sub(r"\s+", " ", text)


def _extract_quotes(wikitext: str) -> list[str]:
    """Parse the subject's own quotes out of a Wikiquote page's wikitext.

    Collects top-level ``*`` bullets, skipping sub-bullets (sources) and any
    section whose heading marks it as quotes-about / disputed / link lists, then
    cleans markup and keeps only sane-length candidates. Order is preserved so a
    window-seeded pick is deterministic.
    """
    quotes: list[str] = []
    skipping = False
    for line in (wikitext or "").splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            title = heading.group(1).lower()
            skipping = any(tok in title for tok in _SKIP_SECTION_TOKENS)
            continue
        if skipping:
            continue
        m = _QUOTE_BULLET_RE.match(line)
        if not m:
            continue
        quote = _clean_markup(m.group(1))
        if (_MIN_QUOTE_CHARS <= len(quote) <= _MAX_QUOTE_CHARS
                and len(quote.split()) >= _MIN_QUOTE_WORDS
                and "=" not in quote):
            quotes.append(quote)
    return quotes


def _fetch_quotes(page: str) -> list[str]:
    """Quotes for a figure's Wikiquote page, or [] on any fetch/parse failure."""
    try:
        wikitext = _fetch_wikitext(page)
    except Exception as exc:  # noqa: BLE001 - network/HTTP failure is non-fatal
        log.warning("Wikiquote fetch failed for %r: %s", page, exc)
        return []
    return _extract_quotes(wikitext)


# --- Figure selection (mirrors the knowledge story engine) ------------------
def _figure_entries() -> list[dict]:
    """Flatten FIGURES into [{id, category, name}] entries with stable ids."""
    return [
        {"id": f"{category}:{name}", "category": category, "name": name}
        for category, names in FIGURES.items()
        for name in names
    ]


def _recent(state: dict, day: _dt.date) -> dict[str, _dt.date]:
    """{figure_id: last-shown date} for ids still inside the no-repeat window."""
    recent: dict[str, _dt.date] = {}
    for figure_id, stamp in (state.get(WIKIQUOTE_SEEN_KEY) or {}).items():
        try:
            seen = _dt.date.fromisoformat(str(stamp))
        except ValueError:
            continue  # malformed stamp: treat as never seen
        if 0 <= (day - seen).days < WIKIQUOTE_REPEAT_DAYS:
            recent[str(figure_id)] = seen
    return recent


def _window(now: _dt.datetime) -> str:
    """The 6-hour-window stamp for a datetime, e.g. '2026-06-16T2'.

    Bucketing the hour keeps the stamp stable when a cron run drifts inside its
    window, so a retry within one spark window re-picks the same figure/quote.
    """
    return f"{now.date().isoformat()}T{now.hour // WIKIQUOTE_WINDOW_HOURS}"


def _pick_figure(state: dict, now: _dt.datetime) -> dict | None:
    """The figure to feature this run, or None when the list is empty.

    Pure selection — does NOT mutate state. Rotates to the next category (sorted,
    cyclic after the one stamped in state) that still has a figure unseen within
    WIKIQUOTE_REPEAT_DAYS, then picks one of its eligible figures with an RNG
    seeded by the current 6-hour window. If every figure was shown recently, the
    least recently shown one is reused so the push is never starved.
    """
    entries = _figure_entries()
    if not entries:
        return None

    recent = _recent(state, now.date())
    categories = sorted({str(e["category"]) for e in entries})
    last = str(state.get(WIKIQUOTE_CATEGORY_KEY, ""))
    start = (categories.index(last) + 1) % len(categories) if last in categories else 0
    rotation = categories[start:] + categories[:start]

    rng = random.Random(_window(now))
    for category in rotation:
        eligible = [e for e in entries
                    if str(e["category"]) == category and str(e["id"]) not in recent]
        if eligible:
            return rng.choice(eligible)
    return min(entries, key=lambda e: recent.get(str(e["id"]), _dt.date.min))


def _pick_quote(quotes: list[str], entry_id: str, now: _dt.datetime) -> str | None:
    """One quote, chosen deterministically per 6-hour window, or None if empty."""
    if not quotes:
        return None
    rng = random.Random(f"{_window(now)}:{entry_id}")
    return rng.choice(quotes)


def _commit(state: dict, chosen: dict, day: _dt.date) -> None:
    """Record `chosen` as shown today and advance the category pointer.

    Rebuilding the seen-map from `recent` also prunes stamps that expired.
    """
    recent = _recent(state, day)
    seen = {figure_id: shown.isoformat() for figure_id, shown in recent.items()}
    seen[str(chosen["id"])] = day.isoformat()
    state[WIKIQUOTE_SEEN_KEY] = seen
    state[WIKIQUOTE_CATEGORY_KEY] = str(chosen["category"])


# --- Narrative generation ---------------------------------------------------
def _clip_story(text: str, limit: int = WIKIQUOTE_CLIP_CHARS) -> str:
    """Trim a story to `limit` chars on a sentence boundary (never mid-word)."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    if cut > limit // 2:
        return head[:cut + 1]
    return head.rsplit(" ", 1)[0].rstrip() + "…"


def _generate_story(person: str, quote: str) -> str | None:
    """A short 2-3 paragraph narrative about a quote via Gemini, or None.

    Delegates to summarize.brief (Gemini first, Anthropic fallback, both
    optional), which never raises and returns None when no provider key is set
    or every call fails — so a flaky/absent LLM skips the push cleanly rather
    than crashing the sweep. The result is clipped so the push stays short.
    """
    prompt = (
        f"This quote was said by {person}: '{quote}'. Write a short narrative "
        "account of who this person was, the context in which they said this, "
        "and why this quote still resonates today. Write exactly 2-3 short "
        "paragraphs as a documentary narrator for an intelligent curious adult. "
        "Do not use bullet points."
    )
    story = summarize.brief(_STORY_SYSTEM, prompt, max_tokens=WIKIQUOTE_STORY_TOKENS)
    return _clip_story(story) if story else None


def _compose(person: str, quote: str, story: str) -> str:
    """The push body: the verbatim quote, its attribution, then the narrative."""
    return f"“{quote}”\n— {person}\n\n{story}"


def send_quote(state: dict, now: _dt.datetime | None = None) -> bool:
    """Send one Gemini-narrated quotation story under the spark topic.

    Called by topics/spark.py when its rotation lands on a quote; the 6-hour
    firing gate lives there. Picks a figure, fetches a real Wikiquote quote,
    then asks Gemini for the narrative. The figure is recorded ONLY after the
    quote is fetched AND a story comes back, so any failure leaves the rotation
    memory untouched (and spark's window unstamped) and the next run retries —
    never crashing the sweep. Returns True when a story was emitted.
    """
    now = now or _dt.datetime.now()
    chosen = _pick_figure(state, now)
    if chosen is None:
        log.warning("spark quote: figure list is empty; skipping")
        return False

    quotes = _fetch_quotes(chosen["name"])
    quote = _pick_quote(quotes, str(chosen["id"]), now)
    if not quote:
        health.source_failed(state, TOPIC,
                             f"no usable Wikiquote quote for {chosen['name']}")
        log.warning("no Wikiquote quote for %r; retrying next run", chosen["name"])
        return False
    health.source_ok(state, TOPIC, data_count=len(quotes))

    story = _generate_story(chosen["name"], quote)
    if not story:
        # Overwrites the source_ok claimed for the quote fetch above (the last
        # report in a run wins): without this, a dead LLM key kept the quote leg
        # looking healthy every window while the push silently never fired.
        log.warning("spark quote story generation failed for %r; retrying next run",
                    chosen["name"])
        health.source_failed(
            state, TOPIC,
            f"story generation failed for {chosen['name']} "
            "(no LLM provider answered)")
        return False

    _commit(state, chosen, now.date())
    events.emit(
        state,
        title=chosen["name"],
        body=_compose(chosen["name"], quote, story),
        topic=TOPIC,
        severity="low",
        source="Wikiquote",
        tags="speech_balloon",
        legacy_priority="low",
        legacy_action="push",
    )
    log.info("sent spark quote story for %r", chosen["name"])
    return True
