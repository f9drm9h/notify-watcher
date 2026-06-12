"""Topic: alert when specific Twitch streamers go live (decapi.me, no key).

decapi.me is a free, no-auth Twitch helper: GET /twitch/uptime/<user> returns
the stream uptime when live and "<user> is offline" otherwise, and /twitch/title
and /twitch/game give context for the alert. We push once per live *session*: a
streamer who is live this run but was not last run gets one notification, and
goes back to being alertable only after they drop offline again. The watchlist
is monitors.json -> twitch.streamers, so it's a config edit, no key, no code.
"""
from __future__ import annotations

import logging

import requests

from .. import config, control, events

log = logging.getLogger(__name__)

STATE_KEY = "twitch_live"  # usernames known live as of the previous run
DECAPI = "https://decapi.me/twitch/{kind}/{user}"
HEADERS = {"User-Agent": "notify-watcher/1.0 (+https://github.com/) personal-use"}

# Substrings that mean "not currently streaming" (or a lookup problem); anything
# else in an uptime response is an actual uptime string, i.e. the channel is live.
_OFFLINE_MARKERS = ("offline", "unknown user", "user not found", "error", "no user")


def _is_live(uptime_text: str) -> bool:
    """True if a decapi /uptime response indicates the channel is live."""
    t = (uptime_text or "").strip().lower()
    if not t:
        return False
    return not any(m in t for m in _OFFLINE_MARKERS)


def _get(kind: str, user: str) -> str:
    resp = requests.get(DECAPI.format(kind=kind, user=user), headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text.strip()


def run(state: dict) -> dict:
    # Config streamers + ones followed from a notification ([Watch streamer]
    # -> state["follows"]["streamers"], docs/design/05), case-insensitive dedupe.
    streamers = list(config.section("twitch").get("streamers") or [])
    seen_users = {s.lower() for s in streamers if isinstance(s, str)}
    for entry in control.follows(state, "streamers"):
        name = str(entry.get("name") or "").strip()
        if name and name.lower() not in seen_users:
            seen_users.add(name.lower())
            streamers.append(name)
    if not streamers:
        log.info("no twitch streamers configured; nothing to do")
        return state

    was_live = set(state.get(STATE_KEY) or [])
    now_live: list[str] = []

    for user in streamers:
        try:
            if not _is_live(_get("uptime", user)):
                continue
            now_live.append(user)
            if user in was_live:
                continue  # already alerted for this session
            # New live session: enrich with title/game, tolerating their failure.
            try:
                title = _get("title", user)
            except Exception:  # noqa: BLE001
                title = ""
            try:
                game = _get("game", user)
            except Exception:  # noqa: BLE001
                game = ""
            body = " - ".join(p for p in (game, title) if p) or "is now live"
            events.emit(
                state,
                title=f"{user} is live on Twitch",
                body=body,
                topic="twitch",
                severity="high",
                source=user,
                click_url=f"https://twitch.tv/{user}",
                tags="purple_circle",
                legacy_priority="high",
                legacy_action="push",
            )
            log.info("twitch: %s went live", user)
        except Exception as exc:  # noqa: BLE001 - isolate each streamer
            log.error("twitch %r check failed: %s", user, exc)
            # Preserve prior live-state on a transient error so we don't re-alert
            # a still-live streamer just because this one poll failed.
            if user in was_live:
                now_live.append(user)

    state[STATE_KEY] = sorted(set(now_live))
    return state
