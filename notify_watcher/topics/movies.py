"""Topic: movie release dates + streaming availability + news + countdowns
for a watchlist.

Four independent checks per title in watchlist.json["movies"]:

1. Release dates (via TMDb). We resolve each title to a TMDb movie and track
   its release date, pushing when first seen and whenever the date moves (a
   delay, or a newly-set date on a previously-TBA film). Needs TMDB_API_KEY;
   if unset this check no-ops quietly.

2. "Now streaming" (via TMDb watch providers). For each title we read the
   subscription (flatrate) providers available in WATCH_REGION and push once
   when a film gains a provider it didn't have before — i.e. when it becomes
   watchable on a service. Rent/buy listings don't count. First run seeds the
   current providers silently, so adding an already-streaming film stays
   quiet. Needs TMDB_API_KEY, same as the release-date check.

3. News / trailers / delays / casting (via Google News RSS). For each title we
   query Google News' free, no-auth RSS search for the exact phrase and push
   any genuinely new article whose headline is specifically about that film.
   Needs no key, so it works even when TMDB_API_KEY is unset.

The three checks are independent and each title is isolated, so one failure (a
TMDb outage, a single bad feed) never blocks the others.

The news path mirrors notify_watcher.topics.games: a quoted Google News query
per title (plus optional aliases) and a token-subset relevance filter that keeps
a search specific. The collected, already-relevance-filtered pool is then handed
to notify_watcher.news.route, the shared scorer/router games uses: each fresh
article is scored against the `movies_scoring` config in monitors.json and
routed by tier. Tuned (2026-06) so only high-signal events push live: a casting
announcement, a release-date move (delay or moved up), a cancellation, or a
real trailer/teaser drop pushes alone from any source, and a leak pushes only
when confirmation language meets a tier1/official source; generic coverage,
rumor pieces, box office, reviews and awards chatter route to the daily digest
or drop. The per-title dedup (capped at NEWS_MAX_PER_MOVIE) and silent
first-run seeding live inside news.route, which records every evaluated id as
seen so a dropped or digested article is never re-scored next run.

4. Weekly countdown. On the first daily run of each ISO week (Monday ~08:00
   DR, same gate as recap), one consolidated push counts down every watchlist
   film with a CONFIRMED release date inside the next COUNTDOWN_WINDOW_DAYS:
   "Avengers: Doomsday releases in 18 days". TBA/undated films are skipped.
   Needs TMDB_API_KEY; deduped per ISO week (``movies_countdown_week``).
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

SEARCH_URL = "https://api.themoviedb.org/3/search/movie"
PROVIDERS_URL = "https://api.themoviedb.org/3/movie/{movie_id}/watch/providers"
MOVIE_PAGE = "https://www.themoviedb.org/movie/"
STATE_KEY = "movie_release_dates"  # { "<tmdb_id>": "YYYY-MM-DD" | "TBA" }
TBA = "TBA"

# --- Weekly release countdown ------------------------------------------------
COUNTDOWN_KEY = "movies_countdown_week"  # ISO week the countdown last fired
COUNTDOWN_WINDOW_DAYS = 60               # only films releasing this soon count

# --- "Now streaming" (TMDb watch providers) ---------------------------------
WATCH_REGION = "DO"  # Dominican Republic; the country whose catalogs we watch
STREAMING_STATE_KEY = "streaming_seen"  # { "<tmdb_id>": [provider_name, ...] }

# --- News (Google News RSS) -------------------------------------------------
NEWS_STATE_KEY = "movie_news_seen"   # { "<title>": [article_id, ...] }
NEWS_MAX_PER_MOVIE = 100             # cap stored ids per movie; feed carries ~100
GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
)
NEWS_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; notify-watcher/1.0)"}

# Optional per-title search aliases. A canonical watchlist title maps to extra
# search phrases so the news watcher also catches headlines that use a common
# alternative name. Each alias is queried separately and the relevance filter
# runs against the *alias* phrase, then all hits merge into the same per-title
# dedup pool. Titles absent here are queried only by their canonical name.
TITLE_ALIASES: dict[str, list[str]] = {
    # Headlines often drop "Spider-Man:" and write just "Beyond the Spider-Verse".
    "Spider-Man: Beyond the Spider-Verse": ["Beyond the Spider-Verse"],
    # The sequel is widely shortened to "The Batman 2" (no "Part II").
    "The Batman Part II": ["The Batman 2"],
}

# Token-subset relevance filter (mirrors games.py). We require every meaningful
# title token to appear in a headline, which keeps a search specific. Stopwords
# are dropped so "of"/"the" don't matter, and common sequel roman numerals are
# mapped to digits so "Part II" still matches a "Part 2" headline.
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


def _news_relevant(phrase: str, headline: str) -> bool:
    """True only if every meaningful token of the search phrase is in the headline.

    Conservative by design: it favours precision over recall, so it skips
    headlines that drop a distinctive word. A missed article is harmless; a
    wrong-film alert is noise.
    """
    want = _news_tokens(phrase)
    return bool(want) and want.issubset(_news_tokens(headline))


def _article_id(entry) -> str:
    """Stable per-article id for dedup: Google News id, else the link."""
    return getattr(entry, "id", "") or getattr(entry, "link", "")


def _entry_source(entry) -> str:
    """Publisher label for an entry, used for provenance weighting in scoring.

    Google News RSS attaches a <source> element (the originating outlet); its
    title is what news._source_weight_key matches against. Returns "" when the
    feed omits it, which scores as the neutral "unknown" tier. Mirrors games.py.
    """
    src = getattr(entry, "source", None)
    if isinstance(src, dict):  # feedparser exposes it as a dict-like
        return src.get("title", "") or ""
    return getattr(src, "title", "") or ""


def _published_key(entry) -> time.struct_time:
    """Sort key for ordering articles newest-first; missing dates sort oldest."""
    return getattr(entry, "published_parsed", None) or time.gmtime(0)


def _search(title: str, api_key: str) -> dict | None:
    """Return the best-match TMDb movie object for a title, or None."""
    resp = requests.get(
        SEARCH_URL,
        params={"api_key": api_key, "query": title, "include_adult": "false"},
        timeout=30,
    )
    resp.raise_for_status()
    results = resp.json().get("results") or []
    # TMDb sorts by popularity/relevance, so the first hit is the intended film.
    return results[0] if results else None


def _track_release_dates(state: dict) -> dict:
    api_key = os.environ.get("TMDB_API_KEY", "").strip()
    if not api_key:
        log.info("TMDB_API_KEY not set; skipping movie watcher")
        return state

    wanted = watchlist.titles("movies", state)
    if not wanted:
        log.info("no movies in watchlist; nothing to do")
        return state

    bucket: dict = state.setdefault(STATE_KEY, {})

    for title in wanted:
        try:
            movie = _search(title, api_key)
            if movie is None:
                log.warning("no TMDb match for movie %r", title)
                continue

            mid = str(movie.get("id"))
            name = movie.get("title") or title
            year = (movie.get("release_date") or "")[:4]
            current = movie.get("release_date") or TBA
            log.info("movie %r -> %s (%s) release %s", title, name, year or "?", current)

            previous = bucket.get(mid)
            if previous == current:
                continue

            ch = None
            if previous is None:
                body = f"Now tracking {name}. Release date: {current}"
            else:
                # Render the day delta via the shared framework (degrades to a string
                # diff if a side is TBA), e.g. "moved from May 26 2027 ... (+115 days)".
                ch = changes.diff(previous, current, kind="date", label=name)
                body = ch.summary
            # A date CHANGE (delay, moved up, or a date landing on a TBA film)
            # is a top-tier event and must push live; under the engine's rules
            # "high" routes to a push while the old "low" scored 15 — BELOW the
            # digest floor — so date changes were being silently dropped.
            # First sight ("now tracking") is informational -> digest.
            state = events.emit(
                state,
                title=f"Movie: {name}",
                body=body,
                change=ch,
                topic="movies",
                severity="moderate" if previous is None else "high",
                source="Movies",
                click_url=MOVIE_PAGE + mid,
                tags="clapper",
                legacy_action="push",
            )
            bucket[mid] = current
        except Exception as exc:  # noqa: BLE001 - isolate each title
            log.error("movie %r check failed: %s", title, exc)

    return state


def _flatrate_providers(movie_id: str, api_key: str) -> list[str]:
    """Subscription-streaming provider names for a movie in WATCH_REGION.

    Reads only the `flatrate` bucket (services where the film is included with
    a subscription); `rent`/`buy` listings exist for most films long before
    streaming and would make "now streaming" meaningless. A missing region or
    empty bucket yields [], which the caller records but never alerts on.
    """
    resp = requests.get(
        PROVIDERS_URL.format(movie_id=movie_id),
        params={"api_key": api_key},
        timeout=30,
    )
    resp.raise_for_status()
    region = (resp.json().get("results") or {}).get(WATCH_REGION) or {}
    names = (p.get("provider_name", "") for p in region.get("flatrate") or [])
    return sorted({n for n in names if n})


def check_streaming(state: dict) -> dict:
    """Push once when a watchlist film becomes streamable in WATCH_REGION.

    Each title resolves to its TMDb movie (same best-match search as the
    release-date check) and we diff the current flatrate providers against the
    set already seen in state. First sight of a film seeds silently, so a
    title that's been on Netflix for years doesn't alert when added. The seen
    set only ever grows (union of every provider observed), so a service that
    drops the film and later re-adds it doesn't re-alert.
    """
    api_key = os.environ.get("TMDB_API_KEY", "").strip()
    if not api_key:
        log.info("TMDB_API_KEY not set; skipping streaming check")
        return state

    wanted = watchlist.titles("movies", state)
    if not wanted:
        log.info("no movies in watchlist; no streaming to check")
        return state

    bucket: dict = state.setdefault(STREAMING_STATE_KEY, {})

    for title in wanted:
        try:
            movie = _search(title, api_key)
            if movie is None:
                log.warning("no TMDb match for movie %r", title)
                continue

            mid = str(movie.get("id"))
            name = movie.get("title") or title
            current = _flatrate_providers(mid, api_key)
            log.info("movie %r streaming in %s: %s", title, WATCH_REGION,
                     ", ".join(current) or "none")

            previous = bucket.get(mid)
            if previous is None:
                bucket[mid] = current  # first sight: seed silently
                continue

            new = [p for p in current if p not in previous]
            if not new:
                continue

            state = events.emit(
                state,
                title=f"Movie: {name}",
                body=(f"🎬 {name} is now streaming on {', '.join(new)} "
                      f"in {WATCH_REGION}"),
                topic="movies",
                severity="high",
                source="Movies",
                click_url=MOVIE_PAGE + mid,
                tags="clapper,tv",
                legacy_action="push",
            )
            bucket[mid] = sorted({*previous, *current})
        except Exception as exc:  # noqa: BLE001 - isolate each title
            log.error("movie %r streaming check failed: %s", title, exc)

    return state


def _fetch_news(phrase: str) -> list:
    """Return Google News RSS entries for an exact-phrase query."""
    url = GOOGLE_NEWS_RSS.format(q=urllib.parse.quote_plus(f'"{phrase}"'))
    resp = requests.get(url, headers=NEWS_HEADERS, timeout=30)
    resp.raise_for_status()
    return feedparser.parse(resp.content).entries


def _collect_news(title: str) -> list[news.Article]:
    """Merge relevant news for a title and each of its aliases into one pool.

    Queries the canonical title first, then every phrase in TITLE_ALIASES for
    it. The relevance filter runs against the phrase that found each article, so
    an alias like "Beyond the Spider-Verse" is matched by its own tokens (which
    the canonical "Spider-Man: Beyond the Spider-Verse" filter would reject for
    omitting "man"). Results are de-duped by article id across all queries, then
    sorted newest-first and truncated to NEWS_MAX_PER_MOVIE so the stored-id
    window stays stable and dedup doesn't re-alert older articles that fall
    outside it. A single phrase's fetch failure is logged and skipped so the
    others still contribute. Returns a list of (article_id, headline, link,
    source); `source` is the publisher used for provenance weighting in scoring.
    """
    max_age = config.section("news").get("max_age_days", news.DEFAULT_MAX_AGE_DAYS)
    merged: dict[str, tuple[time.struct_time, str, str, str, str]] = {}
    for phrase in [title, *TITLE_ALIASES.get(title, [])]:
        try:
            entries = _fetch_news(phrase)
        except Exception as exc:  # noqa: BLE001 - one phrase failing is non-fatal
            log.warning("movie news query %r failed: %s", phrase, exc)
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
        log.info("movie news %r via %r: +%d relevant of %d", title, phrase, kept, len(entries))

    ordered = sorted(merged.values(), key=lambda v: v[0], reverse=True)
    return [(aid, headline, link, source)
            for _, aid, headline, link, source in ordered[:NEWS_MAX_PER_MOVIE]]


def _track_news(state: dict) -> dict:
    """Score and route new, film-specific news per watchlist title.

    Each fresh article is scored deterministically against the `movies_scoring`
    config (release date / trailer / delay headlines -> live push; casting /
    reviews / interviews / set photos / box office -> daily digest; rankings /
    opinion / speculation -> dropped). This replaces the previous behaviour of
    pushing every relevance-matched article live, which was loud for high-
    coverage titles. The scoring + routing + seen-id bookkeeping lives in
    notify_watcher.news, shared with games.

    First run per movie seeds the current article ids silently (no alerts), so a
    brand-new title on the list doesn't blast its backlog; only articles that
    appear afterwards are evaluated. Mirrors games.py.
    """
    wanted = watchlist.titles("movies", state)
    if not wanted:
        log.info("no movies in watchlist; no news to check")
        return state

    bucket: dict = state.setdefault(NEWS_STATE_KEY, {})
    scoring_cfg = config.section("movies_scoring")
    digest_cfg = config.section("digest")

    for title in wanted:
        try:
            relevant = _collect_news(title)
            log.info("movie news %r: %d relevant article(s) across all queries", title, len(relevant))
            news.route(
                state,
                bucket=bucket,
                title=title,
                articles=relevant,
                scoring_cfg=scoring_cfg,
                digest_cfg=digest_cfg,
                cap=NEWS_MAX_PER_MOVIE,
                live_tag="clapper",
                live_title_prefix="Movie news",
                topic="movies",
            )
        except Exception as exc:  # noqa: BLE001 - isolate each title's news check
            log.error("movie news %r check failed: %s", title, exc)

    return state


def _iso_week(day: _dt.date) -> str:
    y, w, _ = day.isocalendar()
    return f"{y}-W{w:02d}"


def _countdown_lines(films: list[tuple[str, str]], today: _dt.date,
                     window_days: int = COUNTDOWN_WINDOW_DAYS) -> list[str]:
    """Pure: countdown lines for films releasing within the window, soonest first.

    ``films`` is (name, release_date) pairs as TMDb returns them. Only a
    CONFIRMED parseable date counts down — TBA, empty, or malformed dates are
    skipped, and so are films already released (negative days) or further out
    than the window.
    """
    upcoming: list[tuple[int, str]] = []
    for name, date_str in films:
        try:
            release = _dt.date.fromisoformat(str(date_str or "")[:10])
        except ValueError:
            continue  # TBA / undated: a countdown needs a confirmed date
        days = (release - today).days
        if 0 <= days <= window_days:
            upcoming.append((days, name))
    upcoming.sort()
    lines = []
    for days, name in upcoming:
        when = ("today" if days == 0 else
                "tomorrow" if days == 1 else f"in {days} days")
        lines.append(f"{name} releases {when}")
    return lines


def _weekly_countdown(state: dict, today: _dt.date | None = None) -> dict:
    """One Monday push counting down watchlist films releasing soon.

    Rides the first daily run of each ISO week (same gate as recap: the
    NOTIFY_DAILY env plus a per-week state stamp, so a dropped Monday cron
    fires on Tuesday instead of skipping the week). Quiet when nothing on the
    watchlist has a confirmed date inside COUNTDOWN_WINDOW_DAYS. The week is
    not stamped while lookups fail and nothing was sent, so a TMDb outage on
    Monday retries on the next daily run instead of losing the week.
    """
    if not os.environ.get("NOTIFY_DAILY"):
        return state  # weekly work rides the daily run, like recap
    today = today or _dt.date.today()
    week = _iso_week(today)
    if state.get(COUNTDOWN_KEY) == week:
        return state
    api_key = os.environ.get("TMDB_API_KEY", "").strip()
    if not api_key:
        log.info("TMDB_API_KEY not set; skipping movie countdown")
        return state
    wanted = watchlist.titles("movies", state)
    if not wanted:
        state[COUNTDOWN_KEY] = week
        return state

    films: list[tuple[str, str]] = []
    failures = False
    for title in wanted:
        try:
            movie = _search(title, api_key)
        except Exception as exc:  # noqa: BLE001 - isolate each title
            failures = True
            log.error("movie countdown lookup %r failed: %s", title, exc)
            continue
        if movie is not None:
            films.append((movie.get("title") or title,
                          movie.get("release_date") or ""))

    lines = _countdown_lines(films, today)
    if lines:
        state = events.emit(
            state,
            title="Movie countdown",
            body="\n".join(lines),
            topic="movies",
            severity="high",  # a deliberate weekly reminder: must ring, not digest
            source="Movies",
            tags="clapper,calendar",
            legacy_priority="low",
            legacy_action="push",
        )
        log.info("movie countdown: sent %d film(s) for %s", len(lines), week)
    if lines or not failures:
        state[COUNTDOWN_KEY] = week
    return state


def run(state: dict) -> dict:
    """Release dates (TMDb) + streaming availability (TMDb) + news (Google
    News) + the Monday countdown, each additive and independently isolated so
    one can fail without affecting the others."""
    state = _track_release_dates(state)
    state = check_streaming(state)
    state = _track_news(state)
    state = _weekly_countdown(state)
    return state
