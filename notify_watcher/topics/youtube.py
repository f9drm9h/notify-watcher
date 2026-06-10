"""Topic: new uploads from followed YouTube channels (free Atom feed, no key).

Every public channel exposes an Atom feed at
https://www.youtube.com/feeds/videos.xml?channel_id=UC... — no API key, no
quota. The watchlist is monitors.json -> youtube.channels (channel_id + name),
so following a new channel is a config edit, not a code change. One push per
new upload. Seen ids are kept per channel ({channel_id: [ids]}), and the
first sight of any channel seeds its current uploads silently, so adding the
topic (or a single channel, at any time) never blasts a backlog. Each channel
is fetched inside its own try/except so one dead feed never blocks the
others, and the whole run is wrapped so a surprise failure can't take down
the sweep.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

import requests

from .. import config, events

log = logging.getLogger(__name__)

STATE_KEY = "youtube_seen"
FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
WATCH_URL = "https://www.youtube.com/watch?v={video_id}"
HEADERS = {"User-Agent": "notify-watcher/1.0 (+https://github.com/) personal-use"}

_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}

# Cap state size per channel: a feed carries only the ~15 most recent uploads,
# so an id this far back can no longer reappear in the feed and re-alert.
MAX_REMEMBERED = 500


def parse_feed(xml_text: str) -> list[tuple[str, str]]:
    """Return [(video_id, title), ...] in feed order from a channel Atom feed."""
    root = ET.fromstring(xml_text)
    videos: list[tuple[str, str]] = []
    for entry in root.findall("atom:entry", _NS):
        vid = (entry.findtext("yt:videoId", default="", namespaces=_NS) or "").strip()
        title = (entry.findtext("atom:title", default="", namespaces=_NS) or "").strip()
        if vid:
            videos.append((vid, title))
    return videos


def _fetch(channel_id: str) -> list[tuple[str, str]]:
    resp = requests.get(FEED_URL.format(channel_id=channel_id),
                        headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return parse_feed(resp.text)


def run(state: dict) -> dict:
    try:
        return _run(state)
    except Exception as exc:  # noqa: BLE001 - this topic must never break the sweep
        log.error("youtube topic failed: %s", exc)
        return state


def _run(state: dict) -> dict:
    channels = config.section("youtube").get("channels") or []
    channels = [c for c in channels
                if isinstance(c, dict) and (c.get("channel_id") or "").strip()]
    if not channels:
        log.info("no youtube channels configured; nothing to do")
        return state

    prior_state = state.get(STATE_KEY)
    # Legacy shape (one flat list across all channels) or a fresh/corrupt
    # state: start from an empty per-channel map. Every channel below is then
    # a first sight and seeds silently, so the migration (like the first run)
    # can't blast a backlog or re-push anything still in a feed.
    if not isinstance(prior_state, dict):
        if isinstance(prior_state, list):
            log.info("migrating %s from flat list to per-channel map "
                     "(re-seeding silently)", STATE_KEY)
        prior_state = {}

    new_seen: dict[str, list[str]] = {}
    pushed = 0
    for channel in channels:
        channel_id = channel["channel_id"].strip()
        name = (channel.get("name") or "").strip() or channel_id
        prior = prior_state.get(channel_id)
        # First sight of this channel: remember its current uploads without
        # pushing, so we alert only on videos published from now on.
        first_sight = prior is None
        seen = list(prior or [])
        seen_set = set(seen)
        try:
            videos = _fetch(channel_id)
        except Exception as exc:  # noqa: BLE001 - isolate each channel
            log.error("youtube %r feed failed: %s", name, exc)
            # Keep an existing channel's memory; leave a first-sight channel
            # absent so it still seeds silently once its feed is reachable.
            if not first_sight:
                new_seen[channel_id] = seen[-MAX_REMEMBERED:]
            continue
        for video_id, title in videos:
            if video_id in seen_set:
                continue
            seen.append(video_id)
            seen_set.add(video_id)
            if first_sight:
                continue  # silent seed: remember the backlog, alert nothing
            events.emit(
                state,
                title=f"{name} uploaded a new video",
                body=title or "(no title)",
                topic="youtube",
                severity="moderate",
                source=name,
                click_url=WATCH_URL.format(video_id=video_id),
                # "tv" is ntfy's shortcode for the TV emoji; a raw emoji can't
                # ride the ASCII Tags header (only Title is RFC 2047-encoded).
                tags="youtube,tv",
                legacy_action="push",
            )
            pushed += 1
        if first_sight:
            log.info("seeded %r with %d video id(s) (silent first sight)",
                     name, len(seen))
        new_seen[channel_id] = seen[-MAX_REMEMBERED:]

    if pushed:
        log.info("pushed %d new YouTube upload(s)", pushed)

    # Channels dropped from the config fall out of state here; re-adding one
    # later makes it a first sight again (silent seed), which is what we want.
    state[STATE_KEY] = new_seen
    return state
