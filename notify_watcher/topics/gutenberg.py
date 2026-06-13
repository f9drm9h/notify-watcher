"""Topic: a Gemini-narrated literary-context story, fresh on every watcher run.

A standalone educational push (not gated on NOTIFY_DAILY). Each run picks one
work from a curated reading list (grouped into genres for even rotation),
fetches a REAL public-domain passage from that book's plain text via Project
Gutenberg / the free Gutendex API (no key), and asks Gemini (via
summarize.brief) to write a rich, multi-paragraph literary and historical
guide: who the author was, when and why they wrote it, what was happening in
the world, where the passage sits in the larger work, what it means, and why it
still resonates. The passage is genuine public-domain text; only the
surrounding narrative is generated.

Design choices mirror the "Knowledge" story engine in topics/learn.py and the
quotation engine in topics/wikiquote.py so the three behave identically and
share the same operational guarantees:

  * Free / no key. Gutendex (https://gutendex.com) and the gutenberg.org plain
    text files need no auth; the reading list is a local Python constant (WORKS).
  * Even genre rotation. A genre pointer in state advances cyclically so
    consecutive pushes never cluster on one shelf.
  * 30-day no-repeat. Each shown work's stable id (``genre:gutenberg_id``) is
    stamped into state.json so no work repeats within GUTENBERG_REPEAT_DAYS.
  * Window-seeded determinism. The in-genre work pick and the passage pick are
    seeded by the current 3-hour window (date + hour bucket), so a re-run inside
    the same window re-picks the same work and passage — safe against the
    runner's repeated/rebased runs — while each new window serves something new.
  * Per-window double-send guard. GUTENBERG_SENT_KEY stamps the window only
    AFTER a story is generated and pushed, so a re-run inside the window is a
    no-op and a failure leaves the window unstamped to retry next run.
  * Graceful skip. If Gutendex is unreachable, the book has no plain-text
    format, the text fetch fails, no sane passage can be extracted, or Gemini
    returns None, the run skips cleanly WITHOUT stamping the window or consuming
    the work — the next run simply retries. A per-topic failure never crashes
    the sweep.
  * LLM optional. summarize.brief tries Gemini first, then Anthropic, and
    returns None when no provider key is set — so an absent/flaky LLM skips the
    push rather than sending a bare passage.
"""
from __future__ import annotations

import datetime as _dt
import logging
import random
import re

import requests

from .. import events, summarize

log = logging.getLogger(__name__)

# --- Gutendex / Project Gutenberg source ------------------------------------
GUTENDEX_URL = "https://gutendex.com/books/{book_id}"
HEADERS = {
    "User-Agent": "notify-watcher/1.0 (personal daily literary digest; +https://github.com/)"
}
# A Gutenberg .txt file can be a megabyte or more; we only need a passage, so
# the download is capped to keep the run light. The boilerplate header sits in
# the first few KB, so a generous cap still leaves plenty of body to sample.
_MAX_TEXT_BYTES = 600_000

# A passage candidate is sane only within this length band: shorter reads as a
# stray fragment, longer overflows a push notification.
_PASSAGE_MIN_CHARS = 350
_PASSAGE_MAX_CHARS = 900
# Skip the front matter (title page, table of contents, preface) and the tail
# (appendices, indexes) by sampling only the inner span of body paragraphs.
_BODY_SKIP_HEAD = 0.12
_BODY_SKIP_TAIL = 0.92

# --- Curated reading list, grouped into genres for even rotation ------------
# Keys are stable genre ids (renaming one resets its 30-day dedup memory). Each
# entry is a famous public-domain work keyed by its Project Gutenberg book id
# (the stable identifier Gutendex resolves); a book that yields no plain text or
# no sane passage is simply skipped that run. The id (``genre:gutenberg_id``)
# drives the 30-day no-repeat window.
WORKS: dict[str, list[dict]] = {
    "novels": [
        {"book_id": 1342, "title": "Pride and Prejudice", "author": "Jane Austen"},
        {"book_id": 1400, "title": "Great Expectations", "author": "Charles Dickens"},
        {"book_id": 98, "title": "A Tale of Two Cities", "author": "Charles Dickens"},
        {"book_id": 1260, "title": "Jane Eyre", "author": "Charlotte Brontë"},
        {"book_id": 768, "title": "Wuthering Heights", "author": "Emily Brontë"},
        {"book_id": 2554, "title": "Crime and Punishment", "author": "Fyodor Dostoevsky"},
        {"book_id": 2600, "title": "War and Peace", "author": "Leo Tolstoy"},
        {"book_id": 64317, "title": "The Great Gatsby", "author": "F. Scott Fitzgerald"},
        {"book_id": 76, "title": "Adventures of Huckleberry Finn", "author": "Mark Twain"},
        {"book_id": 145, "title": "Middlemarch", "author": "George Eliot"},
    ],
    "philosophy_and_essays": [
        {"book_id": 2680, "title": "Meditations", "author": "Marcus Aurelius"},
        {"book_id": 1232, "title": "The Prince", "author": "Niccolò Machiavelli"},
        {"book_id": 1497, "title": "The Republic", "author": "Plato"},
        {"book_id": 132, "title": "The Art of War", "author": "Sun Tzu"},
        {"book_id": 205, "title": "Walden", "author": "Henry David Thoreau"},
        {"book_id": 1080, "title": "A Modest Proposal", "author": "Jonathan Swift"},
        {"book_id": 408, "title": "The Souls of Black Folk", "author": "W. E. B. Du Bois"},
        {"book_id": 3600, "title": "Essays of Montaigne", "author": "Michel de Montaigne"},
        {"book_id": 5827, "title": "The Problems of Philosophy", "author": "Bertrand Russell"},
    ],
    "poetry": [
        {"book_id": 1322, "title": "Leaves of Grass", "author": "Walt Whitman"},
        {"book_id": 1041, "title": "Shakespeare's Sonnets", "author": "William Shakespeare"},
        {"book_id": 8800, "title": "The Divine Comedy", "author": "Dante Alighieri"},
        {"book_id": 6130, "title": "The Iliad", "author": "Homer"},
        {"book_id": 1727, "title": "The Odyssey", "author": "Homer"},
        {"book_id": 12242, "title": "Poems", "author": "Emily Dickinson"},
        {"book_id": 1934, "title": "Songs of Innocence and of Experience", "author": "William Blake"},
        {"book_id": 16328, "title": "Beowulf", "author": "Anonymous"},
        {"book_id": 1065, "title": "The Raven", "author": "Edgar Allan Poe"},
    ],
    "drama": [
        {"book_id": 1524, "title": "Hamlet", "author": "William Shakespeare"},
        {"book_id": 1513, "title": "Romeo and Juliet", "author": "William Shakespeare"},
        {"book_id": 1533, "title": "Macbeth", "author": "William Shakespeare"},
        {"book_id": 844, "title": "The Importance of Being Earnest", "author": "Oscar Wilde"},
        {"book_id": 2542, "title": "A Doll's House", "author": "Henrik Ibsen"},
        {"book_id": 1254, "title": "Cyrano de Bergerac", "author": "Edmond Rostand"},
        {"book_id": 885, "title": "An Ideal Husband", "author": "Oscar Wilde"},
        {"book_id": 1514, "title": "A Midsummer Night's Dream", "author": "William Shakespeare"},
        {"book_id": 3825, "title": "Pygmalion", "author": "George Bernard Shaw"},
    ],
    "adventure_and_science_fiction": [
        {"book_id": 35, "title": "The Time Machine", "author": "H. G. Wells"},
        {"book_id": 36, "title": "The War of the Worlds", "author": "H. G. Wells"},
        {"book_id": 164, "title": "Twenty Thousand Leagues Under the Sea", "author": "Jules Verne"},
        {"book_id": 103, "title": "Around the World in Eighty Days", "author": "Jules Verne"},
        {"book_id": 120, "title": "Treasure Island", "author": "Robert Louis Stevenson"},
        {"book_id": 215, "title": "The Call of the Wild", "author": "Jack London"},
        {"book_id": 1257, "title": "The Three Musketeers", "author": "Alexandre Dumas"},
        {"book_id": 1184, "title": "The Count of Monte Cristo", "author": "Alexandre Dumas"},
        {"book_id": 18857, "title": "A Journey to the Centre of the Earth", "author": "Jules Verne"},
    ],
    "gothic_and_mystery": [
        {"book_id": 84, "title": "Frankenstein", "author": "Mary Shelley"},
        {"book_id": 345, "title": "Dracula", "author": "Bram Stoker"},
        {"book_id": 174, "title": "The Picture of Dorian Gray", "author": "Oscar Wilde"},
        {"book_id": 43, "title": "The Strange Case of Dr. Jekyll and Mr. Hyde", "author": "Robert Louis Stevenson"},
        {"book_id": 1661, "title": "The Adventures of Sherlock Holmes", "author": "Arthur Conan Doyle"},
        {"book_id": 209, "title": "The Turn of the Screw", "author": "Henry James"},
        {"book_id": 2852, "title": "The Hound of the Baskervilles", "author": "Arthur Conan Doyle"},
        {"book_id": 932, "title": "The Fall of the House of Usher", "author": "Edgar Allan Poe"},
        {"book_id": 1064, "title": "The Masque of the Red Death", "author": "Edgar Allan Poe"},
    ],
}

# --- State + windowing ------------------------------------------------------
GUTENBERG_SEEN_KEY = "gutenberg_seen"            # {work_id: "YYYY-MM-DD" last shown}
GUTENBERG_GENRE_KEY = "gutenberg_last_genre"
GUTENBERG_SENT_KEY = "gutenberg_last_sent"        # "YYYY-MM-DDTn" 3-hour window
GUTENBERG_REPEAT_DAYS = 30
GUTENBERG_WINDOW_HOURS = 3  # the watcher cron's cadence

# Story generation budget. ~1024 output tokens comfortably covers three or four
# substantial paragraphs; the result is clipped to GUTENBERG_CLIP_CHARS so a
# long narrative plus the passage stays under ntfy's ~4 KB message limit.
GUTENBERG_STORY_TOKENS = 1024
GUTENBERG_CLIP_CHARS = 2800

_STORY_SYSTEM = (
    "You are a literary guide writing for an intelligent, curious adult. Write "
    "rich, narrative, storytelling prose — never bullet points and never a dry "
    "encyclopedia entry. Write at least three substantial paragraphs. Plain text "
    "only: no markdown, no headings, no lists, no surrounding quotation marks."
)


# --- Gutendex metadata + plain-text fetch -----------------------------------
def _plain_text_url(book_id: int) -> str | None:
    """The book's UTF-8 plain-text URL via the no-key Gutendex API, or None.

    Prefers a ``text/plain; charset=utf-8`` format and never returns a zipped
    archive (those can't be streamed as text). Raises on HTTP error so the
    caller's try/except can treat a fetch failure as "skip this run".
    """
    resp = requests.get(GUTENDEX_URL.format(book_id=book_id), headers=HEADERS, timeout=30)
    resp.raise_for_status()
    formats = resp.json().get("formats") or {}
    plain = {k: v for k, v in formats.items()
             if isinstance(v, str) and k.startswith("text/plain") and not v.endswith(".zip")}
    if not plain:
        return None
    # Prefer an explicit UTF-8 variant; otherwise take any plain-text URL.
    for key, url in plain.items():
        if "utf-8" in key.lower():
            return url
    return next(iter(plain.values()))


def _fetch_text(url: str) -> str:
    """Download (a capped prefix of) a Gutenberg plain-text file. Raises on HTTP error."""
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    text = resp.text
    return text[:_MAX_TEXT_BYTES] if len(text) > _MAX_TEXT_BYTES else text


# Project Gutenberg wraps every text in *** START OF THE PROJECT GUTENBERG
# EBOOK … *** / *** END … *** markers; the body is what sits between them.
_START_RE = re.compile(r"\*\*\*\s*START OF (THE|THIS) PROJECT GUTENBERG.*?\*\*\*", re.IGNORECASE)
_END_RE = re.compile(r"\*\*\*\s*END OF (THE|THIS) PROJECT GUTENBERG.*?\*\*\*", re.IGNORECASE)


def _strip_boilerplate(text: str) -> str:
    """Return only the work's body, dropping Gutenberg's license header/footer."""
    start = _START_RE.search(text)
    if start:
        text = text[start.end():]
    end = _END_RE.search(text)
    if end:
        text = text[:end.start()]
    return text


# Editorial inserts a publisher (not the author) adds to the text: illustration
# captions and bracketed italic/copyright notes like ``[_Copyright 1894…_]``.
# They read poorly in a passage, so they're stripped during normalization. The
# inner ``[_…_]`` note is removed first so the (sometimes nested) ``[Illustration
# … [_…_]]`` caption then has no inner bracket left to confuse the match.
_EDITORIAL_NOTE_RE = re.compile(r"\[_[^\]]*_\]")
_ILLUSTRATION_RE = re.compile(r"\[Illustration[^\[\]]*\]", re.IGNORECASE)


def _strip_editorial(text: str) -> str:
    """Remove publisher illustration captions and bracketed editorial notes."""
    text = _EDITORIAL_NOTE_RE.sub("", text)
    text = _ILLUSTRATION_RE.sub("", text)
    return text


def _paragraphs(body: str) -> list[str]:
    """Split a stripped body into normalized prose paragraphs (blank-line split).

    Runs of text separated by blank lines become single-spaced paragraphs;
    intra-paragraph line breaks (Gutenberg hard-wraps at ~70 cols) are folded
    back into flowing sentences, and publisher editorial inserts are dropped.
    """
    paras: list[str] = []
    for block in re.split(r"\n\s*\n", body):
        para = re.sub(r"\s+", " ", _strip_editorial(block)).strip()
        if para:
            paras.append(para)
    return paras


def _grow_passage(paras: list[str], start: int) -> str:
    """Join paragraphs from `start` until the passage reaches the length band."""
    chunk = ""
    for para in paras[start:]:
        chunk = f"{chunk} {para}".strip() if chunk else para
        if len(chunk) >= _PASSAGE_MIN_CHARS:
            break
    return chunk[:_PASSAGE_MAX_CHARS].rsplit(" ", 1)[0] if len(chunk) > _PASSAGE_MAX_CHARS else chunk


def _extract_passage(text: str, seed: str) -> str | None:
    """A real, sane-length passage from a book's body, chosen per-window, or None.

    Strips the Gutenberg boilerplate, samples only the inner body span (so the
    passage isn't a title page or appendix), and grows a window-seeded run of
    paragraphs into the target length band. Returns None when the body has no
    suitably substantial paragraph (e.g. a fetch that returned only front matter).
    """
    paras = _paragraphs(_strip_boilerplate(text))
    # Only paragraphs long enough to be real prose are valid anchors.
    anchors = [i for i, p in enumerate(paras) if len(p) >= _PASSAGE_MIN_CHARS // 2]
    lo, hi = int(len(paras) * _BODY_SKIP_HEAD), int(len(paras) * _BODY_SKIP_TAIL)
    inner = [i for i in anchors if lo <= i <= hi] or anchors
    if not inner:
        return None
    start = random.Random(seed).choice(inner)
    passage = _grow_passage(paras, start)
    return passage if len(passage) >= _PASSAGE_MIN_CHARS else None


def _fetch_passage(book_id: int, seed: str) -> str | None:
    """A real passage for a Gutenberg work, or None on any fetch/parse failure."""
    try:
        url = _plain_text_url(book_id)
        if not url:
            log.warning("gutenberg book %s has no plain-text format", book_id)
            return None
        text = _fetch_text(url)
    except Exception as exc:  # noqa: BLE001 - network/HTTP failure is non-fatal
        log.warning("gutenberg fetch failed for book %s: %s", book_id, exc)
        return None
    return _extract_passage(text, seed)


# --- Work selection (mirrors the knowledge/quotation story engines) ---------
def _work_entries() -> list[dict]:
    """Flatten WORKS into [{id, genre, book_id, title, author}] with stable ids."""
    return [
        {"id": f"{genre}:{w['book_id']}", "genre": genre, "book_id": int(w["book_id"]),
         "title": str(w["title"]), "author": str(w["author"])}
        for genre, works in WORKS.items()
        for w in works
    ]


def _recent(state: dict, day: _dt.date) -> dict[str, _dt.date]:
    """{work_id: last-shown date} for ids still inside the no-repeat window."""
    recent: dict[str, _dt.date] = {}
    for work_id, stamp in (state.get(GUTENBERG_SEEN_KEY) or {}).items():
        try:
            seen = _dt.date.fromisoformat(str(stamp))
        except ValueError:
            continue  # malformed stamp: treat as never seen
        if 0 <= (day - seen).days < GUTENBERG_REPEAT_DAYS:
            recent[str(work_id)] = seen
    return recent


def _window(now: _dt.datetime) -> str:
    """The 3-hour-window stamp for a datetime, e.g. '2026-06-16T4'.

    Bucketing the hour keeps the stamp stable when a cron run drifts a few
    minutes inside its window, so the seed and the double-send guard agree about
    which window a run belongs to.
    """
    return f"{now.date().isoformat()}T{now.hour // GUTENBERG_WINDOW_HOURS}"


def _pick_work(state: dict, now: _dt.datetime) -> dict | None:
    """The work to feature this run, or None when the reading list is empty.

    Pure selection — does NOT mutate state. Rotates to the next genre (sorted,
    cyclic after the one stamped in state) that still has a work unseen within
    GUTENBERG_REPEAT_DAYS, then picks one of its eligible works with an RNG
    seeded by the current 3-hour window. If every work was shown recently, the
    least recently shown one is reused so the push is never starved.
    """
    entries = _work_entries()
    if not entries:
        return None

    recent = _recent(state, now.date())
    genres = sorted({str(e["genre"]) for e in entries})
    last = str(state.get(GUTENBERG_GENRE_KEY, ""))
    start = (genres.index(last) + 1) % len(genres) if last in genres else 0
    rotation = genres[start:] + genres[:start]

    rng = random.Random(_window(now))
    for genre in rotation:
        eligible = [e for e in entries
                    if str(e["genre"]) == genre and str(e["id"]) not in recent]
        if eligible:
            return rng.choice(eligible)
    return min(entries, key=lambda e: recent.get(str(e["id"]), _dt.date.min))


def _commit(state: dict, chosen: dict, day: _dt.date) -> None:
    """Record `chosen` as shown today and advance the genre pointer.

    Rebuilding the seen-map from `recent` also prunes stamps that expired.
    """
    recent = _recent(state, day)
    seen = {work_id: shown.isoformat() for work_id, shown in recent.items()}
    seen[str(chosen["id"])] = day.isoformat()
    state[GUTENBERG_SEEN_KEY] = seen
    state[GUTENBERG_GENRE_KEY] = str(chosen["genre"])


# --- Narrative generation ---------------------------------------------------
def _clip_story(text: str, limit: int = GUTENBERG_CLIP_CHARS) -> str:
    """Trim a story to `limit` chars on a sentence boundary (never mid-word)."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    if cut > limit // 2:
        return head[:cut + 1]
    return head.rsplit(" ", 1)[0].rstrip() + "…"


def _generate_story(title: str, author: str, passage: str) -> str | None:
    """A rich, multi-paragraph literary/historical context for a passage, or None.

    Delegates to summarize.brief (Gemini first, Anthropic fallback, both
    optional), which never raises and returns None when no provider key is set
    or every call fails — so a flaky/absent LLM skips the push cleanly rather
    than crashing the sweep. The result is clipped to fit ntfy's message limit.
    """
    prompt = (
        f"This passage is from {title} by {author}: '{passage}'. Write a rich "
        "narrative account of: who the author was, when and why they wrote this, "
        "what was happening in the world at the time, where this passage sits in "
        "the larger work, what it means, and why it still resonates today. Write "
        "at least 3 substantial paragraphs as a literary guide for an intelligent "
        "curious adult. Do not use bullet points."
    )
    story = summarize.brief(_STORY_SYSTEM, prompt, max_tokens=GUTENBERG_STORY_TOKENS)
    return _clip_story(story) if story else None


def _compose(title: str, author: str, passage: str, story: str) -> str:
    """The push body: the verbatim passage, its attribution, then the narrative."""
    return f"“{passage}”\n— {title}, {author}\n\n{story}"


def _run(state: dict, now: _dt.datetime | None = None) -> dict:
    """Send one Gemini-narrated literary story per 3-hour window; every cycle.

    Picks a work, fetches a real Gutenberg passage, then asks Gemini for the
    literary/historical context. The work is recorded and the window stamped
    ONLY after the passage is fetched AND a story comes back, so any failure
    leaves both untouched and the next run retries (never crashing the sweep).
    The window stamp makes a re-run inside the same window a no-op.
    """
    now = now or _dt.datetime.now()
    window = _window(now)
    if state.get(GUTENBERG_SENT_KEY) == window:
        log.info("gutenberg push already sent this window; skipping")
        return state

    chosen = _pick_work(state, now)
    if chosen is None:
        log.warning("gutenberg reading list is empty; skipping")
        return state

    passage = _fetch_passage(chosen["book_id"], f"{window}:{chosen['id']}")
    if not passage:
        log.warning("no Gutenberg passage for %r; retrying next run", chosen["title"])
        return state

    story = _generate_story(chosen["title"], chosen["author"], passage)
    if not story:
        log.warning("gutenberg story generation failed for %r; retrying next run",
                    chosen["title"])
        return state

    _commit(state, chosen, now.date())
    events.emit(
        state,
        title=f"{chosen['title']} — {chosen['author']}",
        body=_compose(chosen["title"], chosen["author"], passage, story),
        topic="gutenberg",
        severity="low",
        source="Project Gutenberg",
        tags="books",
        legacy_priority="low",
        legacy_action="push",
    )
    log.info("sent gutenberg story for %r (window %s)", chosen["title"], window)
    state[GUTENBERG_SENT_KEY] = window
    return state


def run(state: dict) -> dict:
    """Topic entry point: narrate one Gutenberg literary story for the current window."""
    return _run(state)
