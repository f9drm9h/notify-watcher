"""Topic: tropical storm / hurricane alerts for our area (NHC, free, no key).

The U.S. National Hurricane Center publishes the Atlantic basin as an ATOM feed.
Most of the year it just says "there are no tropical cyclones", so we stay
silent. When a system threatens our area we route by severity: an entry that
names one of our region_terms AND carries a live_term (a watch/warning) pushes
live; other region-relevant outlook/advisory updates go to the daily digest.
Off-region Atlantic activity never matches a region term, so this stays quiet
unless something is actually pointed at us.

Dedup is by a hash of (link + title + updated time): each new *issuance* about
our region alerts once, while an unchanged repeat of the same advisory is
suppressed. The first run seeds the current region-relevant entries silently.

A live alert attaches the storm's forecast-cone PNG (the ntfy ``Attach``
header renders it inline). The cone URL comes from the entry itself: the
per-storm "Graphics" items embed one verbatim, and every advisory/summary
entry carries the ATCF storm id (e.g. AL132024) from which the URL is
constructed. Deriving it is best-effort — when neither is present the alert
simply goes out without the image.
"""
from __future__ import annotations

import logging
import re

import feedparser
import requests

from .. import config, events, ids

log = logging.getLogger(__name__)

STATE_KEY = "weather_seen_ids"
CAP = 200
DEFAULT_URL = "https://www.nhc.noaa.gov/index-at.xml"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; notify-watcher/1.0)"}

GRAPHICS_BASE = "https://www.nhc.noaa.gov/storm_graphics"
# A cone graphic embedded verbatim in an entry (the per-storm "Graphics" item),
# e.g. https://www.nhc.noaa.gov/storm_graphics/AT13/AL132024_5day_cone_with_line_and_wind_sm2.png
_CONE_PNG_RE = re.compile(r"https?://\S*?/storm_graphics/\S*?cone\S*?\.png", re.IGNORECASE)
# The ATCF storm id, e.g. AL132024 — appears in "Summary for ... (AT3/AL132024)"
# titles and in every public/forecast advisory body. (The AT3 wallet id is NOT
# usable: graphics directories are keyed by storm number, AL13 -> AT13.) A
# lookahead, not a trailing \b: inside graphic filenames the id is followed by
# an underscore (AL132024_wind_probs...), which a \b would reject.
_ATCF_RE = re.compile(r"\b(AL\d{6})(?!\d)", re.IGNORECASE)


def _cone_url(*texts: str) -> str | None:
    """Pure: best-effort forecast-cone PNG URL for a feed entry, or None.

    Prefers a cone URL embedded verbatim in the entry; otherwise derives it
    from the ATCF storm id (AL132024 -> storm_graphics/AT13/AL132024_...png,
    the layout NHC itself embeds in its feed). None when the entry names no
    storm id (the outlook, local statements, the no-cyclones placeholder).
    """
    blob = "\n".join(t or "" for t in texts)
    m = _CONE_PNG_RE.search(blob)
    if m:
        return m.group(0)
    m = _ATCF_RE.search(blob)
    if m:
        atcf = m.group(1).upper()
        return f"{GRAPHICS_BASE}/AT{atcf[2:4]}/{atcf}_5day_cone_with_line_and_wind_sm2.png"
    return None


def _dedup_key(entry) -> str:
    """Stable per-issuance key: link + title + the feed's update/publish stamp."""
    link = getattr(entry, "link", "") or ""
    title = getattr(entry, "title", "") or ""
    stamp = getattr(entry, "updated", "") or getattr(entry, "published", "") or ""
    return f"{link}|{title}|{stamp}"


def _classify(entries, cfg: dict) -> list[tuple]:
    """Pure: keep region-relevant entries, tier each. Returns [(key, tier, title, summary, link)].

    tier is "live" when the entry also carries a live_term (watch/warning),
    otherwise "digest". Entries that don't name a region term are skipped.
    """
    region = [t.lower() for t in cfg.get("region_terms", [])]
    live_terms = [t.lower() for t in cfg.get("live_terms", [])]
    out: list[tuple] = []
    for e in entries:
        title = getattr(e, "title", "") or ""
        summary = getattr(e, "summary", "") or ""
        text = f"{title}\n{summary}".lower()
        if not any(r in text for r in region):
            continue
        tier = "live" if any(t in text for t in live_terms) else "digest"
        out.append((_dedup_key(e), tier, title, summary, getattr(e, "link", "") or ""))
    return out


def run(state: dict) -> dict:
    cfg = config.section("weather")
    url = cfg.get("url") or DEFAULT_URL
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
    except Exception as exc:  # noqa: BLE001 - a fetch/parse failure is non-fatal
        log.error("NHC fetch failed: %s", exc)
        return state

    entries = feed.entries
    classified = _classify(entries, cfg)
    log.info("NHC: %d entr(ies), %d region-relevant", len(entries), len(classified))

    seen = state.get(STATE_KEY)
    if seen is None:
        state[STATE_KEY] = [ids.short(k) for k, *_ in classified][:CAP]
        log.info("seeded %s baseline with %d id(s) (no alerts on first run)",
                 STATE_KEY, len(state[STATE_KEY]))
        return state

    seen = ids.normalize_seen(seen)
    seen_set = set(seen)
    fresh: list[str] = []
    pushed = digested = 0

    for key, tier, title, summary, link in classified:
        h = ids.short(key)
        if h in seen_set:
            continue
        seen_set.add(h)
        fresh.append(h)
        if tier == "live":
            try:
                cone = _cone_url(title, summary, link)
            except Exception:  # noqa: BLE001 - the cone image is decoration; never block the alert
                cone = None
            state = events.emit(
                state,
                title=f"Weather alert: {title}",
                body=(summary[:300] or title),
                topic="weather",
                severity="critical",
                source="Weather",
                click_url=link or None,
                tags="cyclone",
                metadata={"attach_url": cone} if cone else None,
                legacy_priority="urgent",
                legacy_action="push",
            )
            pushed += 1
        else:
            # Score above the entertainment digest tier so a brewing system leads.
            state = events.emit(
                state,
                title=title,
                topic="weather",
                severity="moderate",
                source="Weather",
                click_url=link,
                score=6,
                legacy_action="digest",
            )
            digested += 1

    if pushed or digested:
        log.info("weather: %d live, %d digest", pushed, digested)

    state[STATE_KEY] = (fresh + seen)[:CAP]
    return state
