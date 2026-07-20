"""Discord-native delivery transport — the replacement for the ntfy POST path.

This module owns three things the old ntfy transport never had to:

  1. **Routing.** Every notification carries a ``topic`` (``"fx"``, ``"twitch"``,
     ``"spending"`` …). ``category_for`` maps a topic to one of five categories
     and ``channel_for`` resolves that category to a Discord channel id loaded
     from the environment (``CHANNEL_FINANCE`` etc., see ``CATEGORY_ENV``).
     Anything unmapped falls through to ``CHANNEL_GENERAL`` so a brand-new topic
     is never lost — it just lands in the catch-all channel.

  2. **Rich embeds.** Raw ``title``/``message`` text is rendered into a
     ``discord.Embed`` with a category color (green finance, blue discovery,
     red logs/errors, purple briefing, grey general) and a UTC timestamp.

  3. **Transport.** The embed is delivered with a plain ``requests`` POST to the
     Discord REST API (``POST /channels/{id}/messages``) authenticated with the
     bot token. This is synchronous on purpose: the scheduled sweep
     (``notify_watcher.main``) runs outside any event loop, so it cannot await
     the gateway client the way ``bot.py`` does — it just needs to fire one HTTP
     request and move on.

Configuration (all from the environment / the gitignored ``.env``):

    DISCORD_TOKEN      the bot token (same one bot.py uses)
    CHANNEL_FINANCE    channel id for finance/market topics
    CHANNEL_DISCOVERY  channel id for tech/gaming/media discovery
    CHANNEL_LOGS       channel id for system/errors/watchdog
    CHANNEL_BRIEFING   channel id for the Gemini daily/weekly summaries
    CHANNEL_HABITS     channel id for the daily habit tracker reminders
    CHANNEL_GENERAL    catch-all channel id (default for unmapped topics)

Failure policy mirrors the rest of the codebase: ``send`` *raises* when it is
unconfigured or the API rejects the request, so the digest's "clear the buffer
only after a successful send" contract holds and the per-topic ``try`` in
``main`` logs-and-continues. It never silently swallows a delivery.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import time
from typing import Optional

import requests

try:  # discord.py is a hard dependency (requirements.txt), but degrade loudly.
    import discord
except Exception as exc:  # pragma: no cover - import guard
    raise RuntimeError(
        "discord.py is required for the Discord delivery transport; "
        "install it with `pip install -r requirements.txt`."
    ) from exc

log = logging.getLogger(__name__)

API_BASE = "https://discord.com/api/v10"

# --- Routing ----------------------------------------------------------------
# The five delivery categories and the env var holding each one's channel id.
CATEGORY_ENV = {
    "finance": "CHANNEL_FINANCE",
    "discovery": "CHANNEL_DISCOVERY",
    "logs": "CHANNEL_LOGS",
    "briefing": "CHANNEL_BRIEFING",
    "habits": "CHANNEL_HABITS",
    "general": "CHANNEL_GENERAL",
}

DEFAULT_CATEGORY = "general"

# Discord embed colors per category (int 0xRRGGBB).
CATEGORY_COLOR = {
    "finance": 0x2ECC71,    # green  — money/markets
    "discovery": 0x3498DB,  # blue   — tech/gaming/media finds
    "logs": 0xE74C3C,       # red    — system/errors/watchdog
    "briefing": 0x9B59B6,   # purple — the daily/weekly Gemini summary
    "habits": 0x1ABC9C,     # teal   — daily habit reminders/tracker
    "general": 0x95A5A6,    # grey   — everything else
}

# Topic -> category. Unlisted topics resolve to DEFAULT_CATEGORY ("general"),
# which is exactly the "default any unmatched topic to CHANNEL_GENERAL" rule.
CATEGORY_BY_TOPIC = {
    # --- financial / market -> CHANNEL_FINANCE (green) ---------------------
    "fx": "finance",
    "spending": "finance",
    "bills": "finance",
    "fuel": "finance",
    # --- tech / gaming / media discovery -> CHANNEL_DISCOVERY (blue) -------
    "soundcore_pro": "discovery",
    "twitch": "discovery",
    "music": "discovery",
    "youtube": "discovery",
    "games": "discovery",
    "movies": "discovery",
    "deals": "discovery",
    "groceries": "discovery",
    "golden_sun": "discovery",
    "anthropic_news": "discovery",
    "launches": "discovery",
    # --- Gemini daily / weekly summaries -> CHANNEL_BRIEFING (purple) ------
    "digest": "briefing",
    "recap": "briefing",
    "life_dashboard": "briefing",
    # --- daily habit tracker -> CHANNEL_HABITS (teal) ----------------------
    "habits": "habits",
    # --- system / errors / watchdog -> CHANNEL_LOGS (red) -----------------
    "control": "logs",
    "system": "logs",
    "watchdog": "logs",
    "health": "logs",
    "error": "logs",
}

# Severities that should look like an alarm regardless of the topic's category
# (a critical storm/outage in the general channel still renders red).
_CRITICAL_SEVERITIES = {"critical"}

# A tiny ntfy-tag -> emoji map so the old `tags=` hints still add a visual cue
# in the embed title. Unknown tags are ignored rather than shown as raw text.
_TAG_EMOJI = {
    "white_check_mark": "✅",
    "warning": "⚠️",
    "rotating_light": "🚨",
    "rotating_lights": "🚨",
    "clipboard": "📋",
    "mag": "🔍",
    "alarm_clock": "⏰",
    "money_with_wings": "💸",
    "moneybag": "💰",
    "chart_with_upwards_trend": "📈",
    "chart_with_downwards_trend": "📉",
    "tada": "🎉",
    "bell": "🔔",
    "satellite": "🛰️",
    "video_game": "🎮",
    "shopping_cart": "🛒",
    "fuelpump": "⛽",
    "droplet": "💧",
    "muscle": "💪",
    "lotus": "🧘",
    "broom": "🧹",
    "zzz": "💤",
}

# Discord hard limits.
_MAX_TITLE = 256
_MAX_DESC = 4096


class DiscordConfigError(RuntimeError):
    """Raised when the token or a usable channel id is missing."""


def category_for(topic: Optional[str]) -> str:
    """Return the delivery category for a topic ('general' when unmapped)."""
    return CATEGORY_BY_TOPIC.get((topic or "").strip(), DEFAULT_CATEGORY)


def color_for(topic: Optional[str], severity: Optional[str] = None) -> int:
    """Embed color for a topic, with critical severity overriding to red."""
    if (severity or "") in _CRITICAL_SEVERITIES:
        return CATEGORY_COLOR["logs"]
    return CATEGORY_COLOR.get(category_for(topic), CATEGORY_COLOR[DEFAULT_CATEGORY])


def _channel_id(category: str) -> Optional[str]:
    """Channel id for a category, falling back to CHANNEL_GENERAL."""
    cid = (os.getenv(CATEGORY_ENV.get(category, "")) or "").strip()
    if cid:
        return cid
    return (os.getenv(CATEGORY_ENV[DEFAULT_CATEGORY]) or "").strip() or None


def channel_for(topic: Optional[str]) -> Optional[str]:
    """Resolve a topic straight to its Discord channel id (or None)."""
    return _channel_id(category_for(topic))


def _decorate_title(title: str, tags: Optional[str]) -> str:
    """Prefix the title with emoji for any recognized ntfy tags."""
    title = (title or "(untitled)").strip()
    if not tags:
        return title
    emojis = [
        _TAG_EMOJI[t.strip()]
        for t in str(tags).split(",")
        if t.strip() in _TAG_EMOJI
    ]
    return f"{' '.join(emojis)} {title}".strip() if emojis else title


def build_embed(
    topic: Optional[str],
    title: str,
    message: str,
    *,
    click_url: Optional[str] = None,
    tags: Optional[str] = None,
    severity: Optional[str] = None,
    source: str = "",
    attach_url: Optional[str] = None,
    timestamp: Optional[_dt.datetime] = None,
) -> "discord.Embed":
    """Render one notification into a colored, timestamped discord.Embed."""
    category = category_for(topic)
    embed = discord.Embed(
        title=_decorate_title(title, tags)[:_MAX_TITLE],
        description=(message or "")[:_MAX_DESC] or None,
        color=color_for(topic, severity),
        timestamp=timestamp or _dt.datetime.now(_dt.timezone.utc),
    )
    if click_url:
        embed.url = click_url
    if source:
        embed.set_author(name=str(source)[:_MAX_TITLE])
    if attach_url:
        embed.set_image(url=attach_url)
    footer = f"{topic} · {category}" if topic else category
    if severity and severity not in ("info", "moderate"):
        footer = f"{footer} · {severity}"
    embed.set_footer(text=footer)
    return embed


# A 429 from Discord names how long to wait in `retry_after`. Bursty topics
# (a big grocery sale morning) can trip the per-channel limit; waiting it out
# and retrying keeps the message instead of failing the whole topic run.
_RATE_LIMIT_RETRIES = 3
_RATE_LIMIT_MAX_WAIT = 15.0  # never sleep longer than this per retry


def _post(channel_id: str, embed: "discord.Embed", token: str, timeout: float,
          components: Optional[list] = None) -> Optional[dict]:
    """POST one message; returns Discord's created-message JSON (or None).

    The parsed response is how a caller learns the new message's id — the
    habits tracker stores it so a later run can poll the message's reactions.
    A body that fails to parse yields None rather than an error: delivery
    already succeeded, the id is only an enhancement.
    """
    payload: dict = {"embeds": [embed.to_dict()]}
    if components:
        payload["components"] = components
    for attempt in range(_RATE_LIMIT_RETRIES + 1):
        resp = requests.post(
            f"{API_BASE}/channels/{channel_id}/messages",
            headers={
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json",
                "User-Agent": "notify-watcher (https://github.com, 1.0)",
            },
            json=payload,
            timeout=timeout,
        )
        if resp.status_code != 429 or attempt == _RATE_LIMIT_RETRIES:
            resp.raise_for_status()
            try:
                data = resp.json()
            except Exception:  # noqa: BLE001 - body parse is best-effort
                return None
            return data if isinstance(data, dict) else None
        try:
            wait = float((resp.json() or {}).get("retry_after", 1.0))
        except Exception:  # noqa: BLE001 - malformed body: fall back to 1s
            wait = 1.0
        wait = min(max(wait, 0.5), _RATE_LIMIT_MAX_WAIT)
        log.warning("discord: rate limited (429); retrying in %.1fs (attempt %d/%d)",
                    wait, attempt + 1, _RATE_LIMIT_RETRIES)
        time.sleep(wait)


def send(
    topic: Optional[str],
    title: str,
    message: str,
    *,
    click_url: Optional[str] = None,
    tags: Optional[str] = None,
    severity: Optional[str] = None,
    source: str = "",
    attach_url: Optional[str] = None,
    components: Optional[list] = None,
    timeout: float = 15.0,
) -> Optional[str]:
    """Route `topic` to a channel, render an embed, and POST it to Discord.

    Returns the created message's id (a snowflake string) when Discord's
    response carried one, else None. Callers that don't need it — every
    pre-existing topic — simply ignore the return value.

    ``components`` is an optional list of Discord message-component action rows
    (the native reply buttons built by ``discord_control.actions_to_components``);
    when present they are delivered alongside the embed, otherwise the message is
    embed-only exactly as before.

    Raises DiscordConfigError when the token or a channel id is missing, and
    requests.HTTPError on a non-2xx Discord response, so callers that gate on a
    successful send (digest buffer clearing) and the per-topic try/except in
    main behave exactly as they did with the ntfy transport.
    """
    token = (os.getenv("DISCORD_TOKEN") or "").strip()
    if not token:
        raise DiscordConfigError(
            "DISCORD_TOKEN is not set; cannot deliver to Discord. "
            "Add it to .env (local) or the GitHub Actions secrets (CI)."
        )
    channel_id = channel_for(topic)
    if not channel_id:
        raise DiscordConfigError(
            f"No Discord channel id for topic {topic!r}: set "
            f"{CATEGORY_ENV.get(category_for(topic))} or CHANNEL_GENERAL."
        )

    embed = build_embed(
        topic, title, message,
        click_url=click_url, tags=tags, severity=severity,
        source=source, attach_url=attach_url,
    )
    created = _post(channel_id, embed, token, timeout, components=components)
    log.info("discord: delivered %r to #%s (%s)", title, channel_id, category_for(topic))
    message_id = created.get("id") if isinstance(created, dict) else None
    return str(message_id) if message_id else None


def _headers() -> dict:
    token = (os.getenv("DISCORD_TOKEN") or "").strip()
    if not token:
        raise DiscordConfigError("DISCORD_TOKEN is not set; cannot reach Discord.")
    return {
        "Authorization": f"Bot {token}",
        "User-Agent": "notify-watcher (https://github.com, 1.0)",
    }


def add_reaction(channel_id: str, message_id: str, emoji: str,
                 timeout: float = 15.0) -> None:
    """Add the bot's own reaction to a message (PUT …/reactions/<emoji>/@me).

    The habits tracker uses this to pre-seed the ✅ on each reminder so
    acknowledging is a single tap on an existing reaction. Raises on a missing
    token or a non-2xx response; callers treat failure as cosmetic (the user
    can still add their own reaction).
    """
    from urllib.parse import quote

    resp = requests.put(
        f"{API_BASE}/channels/{channel_id}/messages/{message_id}"
        f"/reactions/{quote(emoji)}/@me",
        headers=_headers(),
        timeout=timeout,
    )
    resp.raise_for_status()


def get_message(channel_id: str, message_id: str,
                timeout: float = 15.0) -> Optional[dict]:
    """Fetch one message's JSON (reactions included), or None on a non-dict body.

    Raises requests.HTTPError on a non-2xx response — a 404 tells the habits
    ack poller the message was deleted and its pending entry should be dropped.
    """
    resp = requests.get(
        f"{API_BASE}/channels/{channel_id}/messages/{message_id}",
        headers=_headers(),
        timeout=timeout,
    )
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001 - malformed body reads as "no data"
        return None
    return data if isinstance(data, dict) else None
