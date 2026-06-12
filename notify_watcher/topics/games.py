"""Topic: video game release dates + news for a personal watchlist.

Two independent checks per game in watchlist.json["games"]:

1. Release dates (via RAWG). We resolve each title to a RAWG game and track
   its release date, pushing when first seen and whenever the date moves
   (e.g. a delay, or a TBA game getting a real date). Needs RAWG_API_KEY;
   if unset this check no-ops quietly.

2. News / trailers / delays (via Google News RSS). For each title we query
   Google News' free, no-auth RSS search for the exact phrase and push any
   genuinely new article whose headline is specifically about that game.
   Needs no key, so it works even when RAWG_API_KEY is unset.

The two checks are independent and each game is isolated, so one failure (a
RAWG outage, a single bad feed) never blocks the others.

--- Why Google News RSS for the news source ---------------------------------
Evaluated against the task's options:
  * RAWG /games/{id} (+ /movies for trailers): would work but only covers
    Rawg-curated description/clip changes, is coupled to RAWG_API_KEY, and
    misses third-party reporting on delays/trailers. Not a real news feed.
  * GiantBomb API/RSS: the API needs a registered key (out of the free/no-key
    goal for this check); its plain RSS isn't filterable per game title.
  * Google News RSS (chosen): free, no auth, no key, returns ~100 recent
    articles per query with a stable id + link, and accepts a quoted exact-
    phrase query so results are already game-specific. A token-subset filter
    (see _news_relevant) then enforces specificity so e.g. "God of War Laufey"
    never matches generic older God of War coverage.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import re
import time
import urllib.parse

import feedparser
import requests

from .. import changes, config, events, news, watchlist

log = logging.getLogger(__name__)

SEARCH_URL = "https://api.rawg.io/api/games"
GAME_PAGE = "https://rawg.io/games/"
STATE_KEY = "game_release_dates"  # { "<rawg_id>": "YYYY-MM-DD" | "TBA" }
TBA = "TBA"

# Games is a *weekly* topic: both checks run once per ISO week to keep game
# updates to a single batched catch-up instead of a constant drip. We stamp the
# ISO week we last ran in state and skip until it rolls over, so the topic fires
# on the first daily run of each week (Monday, or the next available day if
# Monday's run was dropped). WEEK_STATE_KEY guards idempotency across the day's
# repeated post-threshold runs.
WEEK_STATE_KEY = "games_week_last"

# --- News (Google News RSS) -------------------------------------------------
NEWS_STATE_KEY = "game_news_seen"   # { "<title>": [article_id, ...] }
NEWS_MAX_PER_GAME = 100             # cap stored ids per game; feed carries ~100
GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
)
NEWS_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; notify-watcher/1.0)"}

# Optional per-title search aliases. A canonical watchlist title maps to extra
# search phrases so the news watcher also catches headlines that use a common
# abbreviation or alternative name. Each alias is queried separately and the
# relevance filter runs against the *alias* phrase (so a "GTA 6" headline is
# matched by the "GTA 6" tokens, which the canonical "Grand Theft Auto VI"
# filter would reject), then all hits merge into the same per-title dedup pool.
# Titles absent here are queried only by their canonical name, unchanged.
TITLE_ALIASES: dict[str, list[str]] = {
    "Grand Theft Auto VI": ["GTA 6", "GTA VI"],
}

# Token-subset relevance filter. We require every meaningful title token to
# appear in a headline, which is what keeps a search for "God of War Laufey"
# from matching generic "God of War" articles. Stopwords are dropped so "of"
# etc. don't matter, and common sequel roman numerals are mapped to digits so
# "Grand Theft Auto VI" still matches a "Grand Theft Auto 6" headline.
_NEWS_STOPWORDS = {"the", "of", "a", "an", "and", "for", "to", "s", "in", "on", "with", "at"}
_ROMAN_TO_ARABIC = {
    "ii": "2", "iii": "3", "iv": "4", "vi": "6", "vii": "7",
    "viii": "8", "ix": "9", "xi": "11", "xii": "12", "xiii": "13",
}


def _news_tokens(text: str) -> set[str]:
    """Lowercase alphanumeric tokens, stopwords removed, roman numerals mapped."""
    out: set[str] = set()
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        w = _ROMAN_TO_ARABIC.get(w, w)
        if w in _NEWS_STOPWORDS:
            continue
        out.add(w)
    return out


def _news_relevant(title: str, headline: str) -> bool:
    """True only if every meaningful token of the game title is in the headline.

    Conservative by design: it favours precision over recall, so it will skip
    headlines that abbreviate the title ("GTA 6") or drop a franchise word
    ("Wolverine PS5" without "Marvel"). That is the right trade-off here — a
    missed article is harmless, a wrong-game alert is noise.
    """
    want = _news_tokens(title)
    return bool(want) and want.issubset(_news_tokens(headline))


def _article_id(entry) -> str:
    """Stable per-article id for dedup: Google News id, else the link."""
    return getattr(entry, "id", "") or getattr(entry, "link", "")


def _entry_source(entry) -> str:
    """Publisher label for an entry, used for provenance weighting in scoring.

    Google News RSS attaches a <source> element (the originating outlet); its
    title is what news._source_weight_key matches against. Returns "" when the
    feed omits it, which scores as the neutral "unknown" tier.
    """
    src = getattr(entry, "source", None)
    if isinstance(src, dict):  # feedparser exposes it as a dict-like
        return src.get("title", "") or ""
    return getattr(src, "title", "") or ""


def _search(title: str, api_key: str) -> dict | None:
    """Return the best-match RAWG game object for a title, or None."""
    resp = requests.get(
        SEARCH_URL,
        params={
            "key": api_key,
            "search": title,
            "search_precise": "true",
            "page_size": 5,
        },
        timeout=30,
    )
    resp.raise_for_status()
    results = resp.json().get("results") or []
    return results[0] if results else None


def _release(game: dict) -> str:
    """RAWG gives `released` (YYYY-MM-DD or null) and a `tba` flag."""
    if game.get("released"):
        return game["released"]
    return TBA


def _track_release_dates(state: dict) -> dict:
    api_key = os.environ.get("RAWG_API_KEY", "").strip()
    if not api_key:
        log.info("RAWG_API_KEY not set; skipping game watcher")
        return state

    wanted = watchlist.titles("games", state)
    if not wanted:
        log.info("no games in watchlist; nothing to do")
        return state

    bucket: dict = state.setdefault(STATE_KEY, {})

    for title in wanted:
        try:
            game = _search(title, api_key)
            if game is None:
                log.warning("no RAWG match for game %r", title)
                continue

            gid = str(game.get("id"))
            name = game.get("name") or title
            slug = game.get("slug") or ""
            current = _release(game)
            log.info("game %r -> %s release %s", title, name, current)

            previous = bucket.get(gid)
            if previous == current:
                continue

            ch = None
            if previous is None:
                body = f"Now tracking {name}. Release date: {current}"
            else:
                # "how it moved" — the day delta, e.g. "+115 days" (a delay) — via the
                # shared framework; it degrades to a string diff for TBA transitions.
                ch = changes.diff(previous, current, kind="date", label=name)
                body = ch.summary
            # A first-sighting or date change is a top-tier event (release date
            # announced / changed / delayed), so it always rings as a live push.
            state = events.emit(
                state,
                title=f"Game: {name}",
                body=body,
                change=ch,
                topic="games",
                severity="high",
                source=name,
                click_url=GAME_PAGE + slug if slug else None,
                tags="video_game",
                legacy_priority="high",
                legacy_action="push",
            )
            bucket[gid] = current
        except Exception as exc:  # noqa: BLE001 - isolate each title
            log.error("game %r check failed: %s", title, exc)

    return state


def _fetch_news(phrase: str) -> list:
    """Return Google News RSS entries for an exact-phrase query."""
    url = GOOGLE_NEWS_RSS.format(q=urllib.parse.quote_plus(f'"{phrase}"'))
    resp = requests.get(url, headers=NEWS_HEADERS, timeout=30)
    resp.raise_for_status()
    return feedparser.parse(resp.content).entries


def _published_key(entry) -> time.struct_time:
    """Sort key for ordering articles newest-first; missing dates sort oldest."""
    return getattr(entry, "published_parsed", None) or time.gmtime(0)


def _collect_news(title: str) -> list[news.Article]:
    """Merge relevant news for a title and each of its aliases into one pool.

    Queries the canonical title first, then every phrase in TITLE_ALIASES for
    it. The relevance filter runs against the phrase that found each article, so
    an alias like "GTA 6" is matched by its own tokens (which the canonical
    "Grand Theft Auto VI" filter would reject). Results are de-duped by article
    id across all queries so an article surfacing under several phrases is kept
    once, then sorted newest-first and truncated to NEWS_MAX_PER_GAME — aliases
    can push the relevant pool well past the cap, and considering only the newest
    N keeps the stored-id window stable so dedup doesn't re-alert older articles
    that fall outside it. A single phrase's fetch failure is logged and skipped
    so the others still contribute. Returns a list of (article_id, headline,
    link, source); `source` is the publisher used for provenance weighting.
    """
    max_age = config.section("news").get("max_age_days", news.DEFAULT_MAX_AGE_DAYS)
    merged: dict[str, tuple[time.struct_time, str, str, str, str]] = {}
    for phrase in [title, *TITLE_ALIASES.get(title, [])]:
        try:
            entries = _fetch_news(phrase)
        except Exception as exc:  # noqa: BLE001 - one phrase failing is non-fatal
            log.warning("game news query %r failed: %s", phrase, exc)
            continue
        kept = 0
        for e in entries:
            # Google News resurfaces months-old articles under fresh URLs, which
            # defeats id dedup; age-gate them out before they can alert.
            if not news.is_recent(e, max_age):
                continue
            headline = getattr(e, "title", "")
            if not _news_relevant(phrase, headline):
                continue
            aid = _article_id(e)
            if aid and aid not in merged:
                merged[aid] = (_published_key(e), aid, headline,
                               getattr(e, "link", ""), _entry_source(e))
                kept += 1
        log.info("game news %r via %r: +%d relevant of %d", title, phrase, kept, len(entries))

    ordered = sorted(merged.values(), key=lambda v: v[0], reverse=True)
    return [(aid, headline, link, source)
            for _, aid, headline, link, source in ordered[:NEWS_MAX_PER_GAME]]


def _track_news(state: dict) -> dict:
    """Score and route new, game-specific news per watchlist title.

    Each fresh article is scored deterministically against the `games_scoring`
    config (release-date / trailer / reveal / announcement headlines -> live
    push; leaks / interviews / previews / store updates -> daily digest; opinion
    / ranking lists / speculation / passing mentions -> dropped). This replaces
    the previous behaviour of pushing every relevance-matched article live,
    which was loud for high-coverage titles. The scoring + routing + seen-id
    bookkeeping lives in notify_watcher.news so movies can reuse it.

    First run per game seeds the current article ids silently (no alerts), so a
    brand-new game on the list doesn't blast its backlog; only articles that
    appear afterwards are evaluated. Mirrors soundcore_pro's baseline seeding.
    """
    wanted = watchlist.titles("games", state)
    if not wanted:
        log.info("no games in watchlist; no news to check")
        return state

    bucket: dict = state.setdefault(NEWS_STATE_KEY, {})
    scoring_cfg = config.section("games_scoring")
    digest_cfg = config.section("digest")

    for title in wanted:
        try:
            relevant = _collect_news(title)
            log.info("game news %r: %d relevant article(s) across all queries", title, len(relevant))
            news.route(
                state,
                bucket=bucket,
                title=title,
                articles=relevant,
                scoring_cfg=scoring_cfg,
                digest_cfg=digest_cfg,
                cap=NEWS_MAX_PER_GAME,
                live_tag="video_game",
                live_title_prefix="Game news",
                topic="games",
            )
        except Exception as exc:  # noqa: BLE001 - isolate each title's news check
            log.error("game news %r check failed: %s", title, exc)

    return state


def _iso_week(day: _dt.date) -> str:
    """ISO year-week label (e.g. '2026-W24') used to fire at most once a week."""
    iso = day.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def run(state: dict) -> dict:
    """Release-date tracking (RAWG) + news tracking (Google News), each additive
    and independently isolated so one can fail without affecting the other.

    Weekly: acts only on the daily run and only once per ISO week, so game
    updates arrive as a single batched catch-up rather than a constant drip.
    """
    if not os.environ.get("NOTIFY_DAILY"):
        return state  # weekly topic acts only on the daily run
    week = _iso_week(_dt.date.today())
    if state.get(WEEK_STATE_KEY) == week:
        log.info("games already checked this week (%s); skipping", week)
        return state

    state = _track_release_dates(state)
    state = _track_news(state)
    state[WEEK_STATE_KEY] = week
    return state
