"""Topic: Golden Sun community news (wiki news + r/GoldenSun + Google News).

Golden Sun has had no new game since 2010, so "news" is rare and precious: a
remaster/NSO listing, an official Nintendo mention, a major ROM hack. Three
sources feed one pool, scored and routed by the shared news engine
(news.route + the ``golden_sun_scoring`` config), so a remaster announcement
pushes live while ordinary community chatter lands in the daily digest and
memes are dropped. First run seeds the pool silently.

Sources (all in ``monitors.json`` -> ``golden_sun``, no keys):

- **Golden Sun Universe** — the fan wiki's ``Template:News`` dated bullets via
  the MediaWiki API. NOTE: the wiki moved; its old domain goldensununiverse.net
  no longer resolves, the live host is goldensunwiki.net. Bullets older than
  ``wiki_max_age_days`` are ignored so a cosmetic edit to an ancient entry
  (which changes its dedup hash) can never re-alert it.
- **Reddit r/GoldenSun** — top posts of the week. The score-bearing JSON
  endpoint is tried first and filtered to ``reddit_min_score`` (default >50)
  upvotes. Both Reddit fetches send a Reddit-compliant ``User-Agent`` (its API
  rules want an honest, unique agent and actively 403/429 spoofed-browser ones
  from data-center IPs) and retry with exponential backoff that honors any
  ``Retry-After`` header. If the JSON endpoint still refuses, we fall back to
  the top-of-week RSS feed (no scores) and keep its first ``reddit_top_n``
  entries — "top of week" ordering is the closest unauthenticated proxy for the
  same bar.
- **Google News** — exact-phrase "Golden Sun" game search, age-gated like the
  other news topics and kept only when the headline itself names the game.

Everything downstream — scoring, push/digest/drop, per-id dedup, silent
seeding — is news.route, exactly as games/movies use it. A fetch failure in
any source is logged and skipped so the others still report; if every source
fails before the first seed, seeding is deferred to the next healthy run so a
dead first run can't make the next one blast a backlog.
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
import time
import urllib.parse

import feedparser
import requests

from .. import config, news

log = logging.getLogger(__name__)

STATE_KEY = "golden_sun_seen"   # news.route bucket: {"Golden Sun": [id, ...]}
TITLE = "Golden Sun"
CAP = 150
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
DEFAULT_QUERY = '"Golden Sun" game'
DEFAULT_WIKI_API = ("https://goldensunwiki.net/w/api.php"
                    "?action=parse&page=Template:News&prop=wikitext&format=json")
WIKI_NEWS_URL = "https://goldensunwiki.net/wiki/Template:News"
DEFAULT_REDDIT_JSON = "https://www.reddit.com/r/GoldenSun/top.json?t=week&limit=25"
DEFAULT_REDDIT_RSS = "https://www.reddit.com/r/GoldenSun/top/.rss?t=week"
DEFAULT_WIKI_MAX_AGE_DAYS = 60
DEFAULT_REDDIT_MIN_SCORE = 50
DEFAULT_REDDIT_TOP_N = 3
REDDIT_JSON_CAP = 10  # even score-qualified posts: top week can't flood the pool
# The wiki's WAF and Google News reject obvious bot agents; a desktop UA passes.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}
# Reddit is the opposite: its API rules ask for a unique, descriptive User-Agent
# and explicitly tell clients NOT to spoof a browser — the desktop UA above is
# exactly what its bot defenses 403/429 from data-center IPs (the GitHub Actions
# runner). Identify honestly in the recommended
# "<platform>:<app id>:<version> (by /u/<user>)" form. See
# https://github.com/reddit-archive/reddit/wiki/API#rules
REDDIT_HEADERS = {
    "User-Agent": "github-actions:notify-watcher:1.0 (by /u/f9drm9h)",
    "Accept": "application/json, application/atom+xml;q=0.9, */*;q=0.8",
}
_REDDIT_ATTEMPTS = 3
_REDDIT_BACKOFF = 2.0     # base seconds, doubled each retry: 2s, 4s
_REDDIT_MAX_SLEEP = 30.0  # cap so a long Retry-After can't stall the whole run

# Wiki templates that render game titles inside news bullets.
_WIKI_TEMPLATES = {
    "GSTitle": "Golden Sun",
    "GSTLATitle": "Golden Sun: The Lost Age",
    "GSDDTitle": "Golden Sun: Dark Dawn",
}
_WIKI_BULLET = re.compile(r"^\*\s*'''(\d{1,2}/\d{1,2}/\d{4}):?'''\s*(.+)$")


def _clean_wikitext(text: str) -> str:
    """Render a news bullet's wikitext as plain text.

    Known title templates expand to their game names, unknown ones vanish;
    ``[[page|label]]``/``[[page]]`` keep the visible part; external
    ``[url label]`` keeps the label; bold/italic quote runs are dropped.
    """
    text = re.sub(
        r"\{\{(\w+)\}\}",
        lambda m: _WIKI_TEMPLATES.get(m.group(1), ""),
        text,
    )
    text = re.sub(r"\[\[:?[^\]|]*\|([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\[\[:?([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\[\S+\s+([^\]]+)\]", r"\1", text)
    text = text.replace("'''", "").replace("''", "")
    return re.sub(r"\s+", " ", text).strip()


def _parse_wiki_news(wikitext: str, max_age_days: float,
                     today: _dt.date | None = None) -> list[tuple[str, str]]:
    """Pure: recent news bullets as [(id, headline)], newest kept first.

    Only the live section is read — everything from ``<noinclude>`` on is the
    archive. Each bullet's US-style M/D/YYYY date both prefixes the headline
    and age-gates the item; undated or unparseable lines are skipped (the
    template's prose footer would otherwise leak in). The id hashes the cleaned
    text, so the same bullet never re-alerts but a genuinely new one does.
    """
    live = wikitext.split("<noinclude>", 1)[0]
    today = today or _dt.date.today()
    out: list[tuple[str, str]] = []
    for line in live.splitlines():
        m = _WIKI_BULLET.match(line.strip())
        if not m:
            continue
        date_s, body = m.groups()
        try:
            mm, dd, yyyy = (int(p) for p in date_s.split("/"))
            posted = _dt.date(yyyy, mm, dd)
        except ValueError:
            continue
        if (today - posted).days > max_age_days:
            continue
        text = _clean_wikitext(body)
        if not text:
            continue
        out.append((f"gsu:{text}", f"{date_s}: {text}"))
    return out


def _parse_reddit_json(payload: dict, min_score: int) -> list[tuple[str, str, str]]:
    """Pure: posts with score > ``min_score`` as [(id, title, link)].

    Reads the listing endpoint's shape (data.children[].data); malformed
    children are skipped, never raised on.
    """
    out: list[tuple[str, str, str]] = []
    try:
        children = payload["data"]["children"]
    except (KeyError, TypeError):
        return out
    if not isinstance(children, list):
        return out
    for child in children:
        d = child.get("data") if isinstance(child, dict) else None
        if not isinstance(d, dict):
            continue
        name, title = d.get("name"), d.get("title")
        try:
            score = int(d.get("score"))
        except (ValueError, TypeError):
            continue
        if not name or not title or score <= min_score:
            continue
        out.append((name, title, f"https://www.reddit.com{d.get('permalink', '')}"))
        if len(out) >= REDDIT_JSON_CAP:
            break
    return out


def _relevant(headline: str) -> bool:
    """Google News matches article bodies too; keep only headlines that name
    the game themselves, so a passing mention can't alert."""
    return "golden sun" in (headline or "").lower()


def _collect_wiki(cfg: dict) -> list[news.Article]:
    api = cfg.get("wiki_api", DEFAULT_WIKI_API)
    max_age = float(cfg.get("wiki_max_age_days", DEFAULT_WIKI_MAX_AGE_DAYS))
    resp = requests.get(api, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    wikitext = resp.json()["parse"]["wikitext"]["*"]
    items = _parse_wiki_news(wikitext, max_age)
    return [(iid, headline, WIKI_NEWS_URL, "Golden Sun Universe")
            for iid, headline in items]


def _retry_after_seconds(resp: "requests.Response | None") -> float | None:
    """Seconds to wait from a 429/503 ``Retry-After`` header, when numeric.

    Reddit usually sends a small integer count of seconds; the HTTP-date form is
    ignored here so we just fall back to plain exponential backoff instead of
    doing calendar math.
    """
    if resp is None:
        return None
    raw = resp.headers.get("Retry-After")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _reddit_get(url: str, *, timeout: int = 30) -> requests.Response:
    """GET a Reddit URL with a compliant UA and exponential backoff.

    Retries rate-limit and transient responses (HTTP 429 and 5xx) plus
    connection errors, honoring a numeric ``Retry-After`` when present (capped so
    a long cooloff can't stall the run), then raises for status so the caller's
    JSON -> RSS fallback still triggers when Reddit stays unavailable.
    """
    last_exc: Exception | None = None
    for attempt in range(_REDDIT_ATTEMPTS):
        resp: requests.Response | None = None
        try:
            resp = requests.get(url, headers=REDDIT_HEADERS, timeout=timeout)
            if resp.status_code not in (429, 500, 502, 503, 504):
                resp.raise_for_status()
                return resp
            last_exc = requests.HTTPError(
                f"{resp.status_code} from Reddit", response=resp)
        except requests.RequestException as exc:
            last_exc = exc
            resp = getattr(exc, "response", None)
        if attempt < _REDDIT_ATTEMPTS - 1:
            wait = _retry_after_seconds(resp)
            if wait is None:
                wait = _REDDIT_BACKOFF * (2 ** attempt)
            wait = min(wait, _REDDIT_MAX_SLEEP)
            log.info("golden_sun: reddit %s; retrying in %.0fs (%d/%d)",
                     last_exc, wait, attempt + 1, _REDDIT_ATTEMPTS)
            time.sleep(wait)
    raise last_exc if last_exc else RuntimeError("reddit unreachable")


def _collect_reddit(cfg: dict) -> list[news.Article]:
    """Score-filtered JSON first; top-of-week RSS top-N when Reddit blocks it.

    Both fetches go through ``_reddit_get`` (compliant UA + backoff), so a 403
    (bot UA) or 429 (rate limit) gets a real chance to recover before we drop to
    the unscored RSS path."""
    min_score = int(cfg.get("reddit_min_score", DEFAULT_REDDIT_MIN_SCORE))
    try:
        resp = _reddit_get(cfg.get("reddit_json", DEFAULT_REDDIT_JSON))
        posts = _parse_reddit_json(resp.json(), min_score)
        return [(rid, title, link, "Reddit r/GoldenSun")
                for rid, title, link in posts]
    except Exception as exc:  # noqa: BLE001 - expected: Reddit 403s bot IPs
        log.info("golden_sun: reddit JSON unavailable (%s); using RSS fallback", exc)
    resp = _reddit_get(cfg.get("reddit_rss", DEFAULT_REDDIT_RSS))
    entries = feedparser.parse(resp.content).entries
    top_n = int(cfg.get("reddit_top_n", DEFAULT_REDDIT_TOP_N))
    out: list[news.Article] = []
    for e in entries[:top_n]:
        rid = getattr(e, "id", "") or getattr(e, "link", "")
        if rid:
            out.append((rid, getattr(e, "title", ""), getattr(e, "link", ""),
                        "Reddit r/GoldenSun"))
    return out


def _collect_google_news(cfg: dict) -> list[news.Article]:
    query = cfg.get("query", DEFAULT_QUERY)
    url = GOOGLE_NEWS_RSS.format(q=urllib.parse.quote_plus(query))
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    entries = feedparser.parse(resp.content).entries
    max_age = config.section("news").get("max_age_days", news.DEFAULT_MAX_AGE_DAYS)
    out: list[news.Article] = []
    for e in entries:
        if not news.is_recent(e, max_age):
            continue
        headline = getattr(e, "title", "")
        if not _relevant(headline):
            continue
        aid = getattr(e, "id", "") or getattr(e, "link", "")
        if aid:
            src = getattr(e, "source", None)
            source = (src.get("title", "") if isinstance(src, dict)
                      else getattr(src, "title", "") or "")
            out.append((aid, headline, getattr(e, "link", ""), source))
    return out


def run(state: dict) -> dict:
    cfg = config.section("golden_sun")
    articles: list[news.Article] = []
    for label, collect in (("wiki", _collect_wiki),
                           ("reddit", _collect_reddit),
                           ("google news", _collect_google_news)):
        try:
            got = collect(cfg)
            articles.extend(got)
            log.info("golden_sun: %s -> %d item(s)", label, len(got))
        except Exception as exc:  # noqa: BLE001 - one source failing is non-fatal
            log.warning("golden_sun: %s fetch failed: %s", label, exc)

    bucket = state.setdefault(STATE_KEY, {})
    if not articles and bucket.get(TITLE) is None:
        # Every source failed before the first seed: seeding an EMPTY baseline
        # would make the next healthy run treat the whole backlog as fresh.
        log.warning("golden_sun: nothing fetched on first run; seeding deferred")
        return state

    news.route(
        state,
        bucket=bucket,
        title=TITLE,
        articles=articles,
        scoring_cfg=config.section("golden_sun_scoring"),
        digest_cfg=config.section("digest"),
        cap=CAP,
        live_tag="sun_with_face",
        live_title_prefix="Golden Sun",
        topic="golden_sun",
    )
    return state
