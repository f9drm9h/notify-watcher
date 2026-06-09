"""Topic: new iOS / iPadOS software releases from Apple Developer Releases RSS.

Apple publishes every shipping build (iOS, iPadOS, macOS, ...) to a free RSS
feed as it goes out. We keep the public stable iOS/iPadOS releases — dropping
betas and release candidates — and push once per new build so you learn an
update is available without opening Settings.

The notification body is a one-line AI "is it worth installing now?" take when a
provider key (GEMINI_API_KEY, then ANTHROPIC_API_KEY) is set, and falls back to
the version + build line otherwise. Note: the RSS entry only carries the version
and build number, not the full release notes, so the AI take is a quick steer
(major vs. minor/security point release), not a changelog — tap through the
linked release-notes page for detail.
"""
from __future__ import annotations

import logging

import feedparser

from .. import events, summarize

log = logging.getLogger(__name__)

FEED_URL = "https://developer.apple.com/news/releases/rss/releases.rss"
STATE_KEY = "ios_seen_builds"
# Which platforms to alert on. Titles look like "iOS 18.5 (22F76)".
PLATFORMS = ("ios", "ipados")
# Drop pre-release builds: we only care about what's safe to install now.
SKIP_MARKERS = ("beta", "release candidate", " rc ", "(rc")
MAX_REMEMBERED = 200  # cap state size; the feed only carries recent items

_SUMMARY_SYSTEM = (
    "You write one-line push-notification advice about a new Apple iOS/iPadOS "
    "software release. You are given only the release title (version and build "
    "number); you do NOT have the changelog. Reply with a single plain-text "
    "sentence of at most ~30 words: state the version, and from the version "
    "number infer whether it looks like a major feature update (e.g. x.0) or a "
    "minor/security point release worth installing soon. Output only that "
    "sentence: no preamble, no markdown, no quotation marks."
)


def _is_wanted(title: str) -> bool:
    t = title.lower()
    if any(marker in t for marker in SKIP_MARKERS):
        return False
    return any(t.startswith(p) for p in PLATFORMS)


def _body(title: str) -> str:
    """One-line AI take, or the plain title when no provider/key is available."""
    return summarize.one_line(_SUMMARY_SYSTEM, f"Release: {title}") or title


def _entry_id(entry) -> str:
    """Stable identifier for dedup: the title carries version+build, so it is
    unique per release; fall back to the link or feed id."""
    return (
        getattr(entry, "title", "")
        or getattr(entry, "link", "")
        or getattr(entry, "id", "")
    )


def run(state: dict) -> dict:
    feed = feedparser.parse(FEED_URL)
    if getattr(feed, "bozo", 0) and not feed.entries:
        raise RuntimeError(f"feed parse failed: {getattr(feed, 'bozo_exception', '')}")

    matched = [e for e in feed.entries if _is_wanted(getattr(e, "title", ""))]
    log.info("feed entries: %d, iOS/iPadOS releases: %d", len(feed.entries), len(matched))

    seen = state.get(STATE_KEY)
    if seen is None:
        # First run (or a reset/corrupt state): seed the current releases silently
        # so we never blast the feed's backlog. Mirrors the seeding every other
        # topic does; without it, a wiped state.json would re-push every shipping
        # iOS/iPadOS build the feed still carries at once.
        state[STATE_KEY] = [eid for e in matched if (eid := _entry_id(e))][-MAX_REMEMBERED:]
        log.info("seeded %s baseline with %d id(s) (no alerts on first run)",
                 STATE_KEY, len(state[STATE_KEY]))
        return state

    seen = list(seen)
    seen_set = set(seen)

    pushed = 0
    for entry in matched:
        title = getattr(entry, "title", "")
        eid = _entry_id(entry)
        if not eid or eid in seen_set:
            continue
        link = getattr(entry, "link", "")
        state = events.emit(
            state,
            title=f"Apple release: {title}",
            body=_body(title),
            topic="ios_release",
            severity="moderate",
            source="Apple",
            click_url=link or None,
            tags="iphone",
            legacy_action="push",
        )
        seen.append(eid)
        seen_set.add(eid)
        pushed += 1

    if pushed:
        log.info("pushed %d new iOS/iPadOS release(s)", pushed)

    # Keep only the most recent N IDs so state.json stays small.
    state[STATE_KEY] = seen[-MAX_REMEMBERED:]
    return state
