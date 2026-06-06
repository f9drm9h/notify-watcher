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

import logging
import os
import re
import urllib.parse

import feedparser
import requests

from .. import ntfy, watchlist

log = logging.getLogger(__name__)

SEARCH_URL = "https://api.rawg.io/api/games"
GAME_PAGE = "https://rawg.io/games/"
STATE_KEY = "game_release_dates"  # { "<rawg_id>": "YYYY-MM-DD" | "TBA" }
TBA = "TBA"

# --- News (Google News RSS) -------------------------------------------------
NEWS_STATE_KEY = "game_news_seen"   # { "<title>": [article_id, ...] }
NEWS_MAX_PER_GAME = 100             # cap stored ids per game; feed carries ~100
GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
)
NEWS_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; notify-watcher/1.0)"}

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

    wanted = watchlist.titles("games")
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

            if previous is None:
                body = f"Now tracking {name}. Release date: {current}"
            else:
                body = f"{name} release date changed: {previous} -> {current}"
            ntfy.push(
                title=f"Game: {name}",
                message=body,
                click_url=GAME_PAGE + slug if slug else None,
                tags="video_game",
            )
            bucket[gid] = current
        except Exception as exc:  # noqa: BLE001 - isolate each title
            log.error("game %r check failed: %s", title, exc)

    return state


def _fetch_news(title: str) -> list:
    """Return Google News RSS entries for an exact-phrase title query."""
    url = GOOGLE_NEWS_RSS.format(q=urllib.parse.quote_plus(f'"{title}"'))
    resp = requests.get(url, headers=NEWS_HEADERS, timeout=30)
    resp.raise_for_status()
    return feedparser.parse(resp.content).entries


def _track_news(state: dict) -> dict:
    """Push new, game-specific news/trailer/delay articles per watchlist title.

    First run per game seeds the current article ids silently (no alerts), so a
    brand-new game on the list doesn't blast ~100 back-dated notifications; only
    articles that appear afterwards are pushed. Mirrors soundcore_pro's baseline
    seeding.
    """
    wanted = watchlist.titles("games")
    if not wanted:
        log.info("no games in watchlist; no news to check")
        return state

    bucket: dict = state.setdefault(NEWS_STATE_KEY, {})

    for title in wanted:
        try:
            entries = _fetch_news(title)
            # Relevant, deduped-by-id, newest-first (Google News default order).
            relevant: list[tuple[str, str, str]] = []
            for e in entries:
                headline = getattr(e, "title", "")
                if not _news_relevant(title, headline):
                    continue
                aid = _article_id(e)
                if aid:
                    relevant.append((aid, headline, getattr(e, "link", "")))
            log.info("game news %r: %d relevant of %d", title, len(relevant), len(entries))

            seen = bucket.get(title)
            if seen is None:
                # Baseline-only first run: remember without alerting.
                bucket[title] = [aid for aid, _, _ in relevant][:NEWS_MAX_PER_GAME]
                log.info("seeded news baseline for %r (no alerts on first run)", title)
                continue

            seen_set = set(seen)
            fresh: list[str] = []
            for aid, headline, link in relevant:
                if aid in seen_set:
                    continue
                ntfy.push(
                    title=f"Game news: {title}",
                    message=headline,
                    click_url=link or None,
                    tags="newspaper",
                )
                fresh.append(aid)
                seen_set.add(aid)
            if fresh:
                log.info("pushed %d new article(s) for %r", len(fresh), title)
            # Keep newest-first and cap so state.json stays small.
            bucket[title] = (fresh + seen)[:NEWS_MAX_PER_GAME]
        except Exception as exc:  # noqa: BLE001 - isolate each title's news check
            log.error("game news %r check failed: %s", title, exc)

    return state


def run(state: dict) -> dict:
    """Release-date tracking (RAWG) + news tracking (Google News), each additive
    and independently isolated so one can fail without affecting the other."""
    state = _track_release_dates(state)
    state = _track_news(state)
    return state
