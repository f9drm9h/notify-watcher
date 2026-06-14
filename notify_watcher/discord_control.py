"""Discord-native two-way control channel — the replacement for the ntfy one.

Delivery moved to Discord rich embeds (``discord_delivery``); this module moves
the *control loop* there too, so ntfy is no longer in the path at all. The idea
is identical to the old ntfy design (``control``): a single private command
queue that both ends can reach. The only thing that changes is the transport —
ntfy.sh becomes one private Discord channel (``DISCORD_CONTROL_CHANNEL``):

  * The always-on gateway bot (``bot.py``) turns a button tap into a command
    string (``MUTE:movies:24``) and POSTs it to the control channel.
  * The scheduled sweep (``notify_watcher.main``) drains that channel at the top
    of every run with one REST GET and feeds each command into the EXISTING
    :func:`notify_watcher.control.dispatch`. The command grammar, the handlers,
    and the ``state["muted"]`` enforcement in ``events.emit`` are all reused
    unchanged — only where the bytes arrive from is different.

Because the runner reads the channel directly, **free-text admin commands typed
straight into the channel** (``status movies``, ``explain fx``) work with no bot
running at all; only *button* taps need ``bot.py`` up to translate the
interaction into a channel message.

This module owns two seams:

  * **Outbound** — :func:`actions_to_components` turns the transport-neutral
    button descriptors (``{"label", "command"}``) every topic already builds via
    :func:`notify_watcher.control.make_action` into Discord message components,
    so the reply buttons render natively under the embed.
  * **Inbound** — :func:`poll` drains the control channel into command strings
    for :func:`notify_watcher.control.dispatch`.

Kill switch: an unset/empty ``DISCORD_CONTROL_CHANNEL`` (or missing
``DISCORD_TOKEN``) disables everything — :func:`poll` returns ``[]`` and
:func:`actions_to_components` returns ``None`` so no components are attached,
leaving delivery byte-identical to a build without this module.

Safety mirrors ``control``: commands are only ever *dispatched* by
``control.dispatch`` (strict per-verb grammar, never executed here), a per-poll
cap, a cursor so each message is handled once, silent first-run seeding so the
channel backlog is never replayed, and network errors that log-and-return ``[]``
so the control loop can never block or crash the sweep.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import requests

from . import discord_delivery

log = logging.getLogger(__name__)

API_BASE = discord_delivery.API_BASE

# custom_id namespace for our buttons: ``nw|<command>`` where <command> is the
# very same grammar control.dispatch understands (MUTE:movies:24, READ:<id>...).
# Discord caps a custom_id at 100 chars; every command we emit is far shorter.
CUSTOM_ID_PREFIX = "nw|"

# Discord button styles (component type 2): 1 primary/blurple, 2 secondary/grey,
# 3 success/green, 4 danger/red.
_STYLE_PRIMARY, _STYLE_SECONDARY, _STYLE_SUCCESS, _STYLE_DANGER = 1, 2, 3, 4

# Per-verb button color, so a mute reads as destructive (red) and a follow/done
# as a positive confirmation (green). Anything unlisted is low-key grey.
_STYLE_BY_VERB = {
    "MUTE": _STYLE_DANGER,
    "DONE": _STYLE_SUCCESS,
    "FOLLOW": _STYLE_SUCCESS,
    "ADD": _STYLE_SUCCESS,
}

# At most five buttons fit in one Discord action row; we never emit more than one
# row, so this doubles as the hard per-push cap.
MAX_BUTTONS = 5
# Bound how many channel messages one poll will dispatch, so a flooded control
# channel can't stall a run (mirrors control.MAX_PER_POLL).
MAX_PER_POLL = 50
# How many messages to pull per poll (Discord allows up to 100).
_FETCH_LIMIT = 100


def _token() -> str:
    return (os.getenv("DISCORD_TOKEN") or "").strip()


def _control_channel() -> str:
    return (os.getenv("DISCORD_CONTROL_CHANNEL") or "").strip()


def enabled() -> bool:
    """True only when both a bot token and a control channel id are configured.

    This is the single kill switch: with it False, no buttons are rendered and
    no channel is polled, so the whole control loop is inert.
    """
    return bool(_token()) and bool(_control_channel())


# --- Outbound: descriptors -> Discord components -----------------------------

def _command_of(action: object) -> str:
    """Pull the command string out of a button descriptor.

    Accepts the neutral ``{"command": ...}`` shape control.make_action now emits
    and, defensively, the old ntfy http-action shape that carried the command in
    ``"body"`` — so a stray legacy descriptor still renders rather than vanishing.
    """
    if not isinstance(action, dict):
        return ""
    return str(action.get("command") or action.get("body") or "").strip()


def _style_for(command: str) -> int:
    verb = command.split(":", 1)[0]
    return _STYLE_BY_VERB.get(verb, _STYLE_SECONDARY)


def make_button(label: str, command: str, *, style: Optional[int] = None) -> dict:
    """One Discord button component carrying ``command`` in its custom_id.

    The custom_id is ``nw|<command>``; the always-on bot strips the prefix and
    relays the command verbatim to the control channel, where the next sweep
    dispatches it. No state or payload travels in the id — only a reference the
    handlers already know how to resolve (a topic slug or a 16-hex event id).
    """
    return {
        "type": 2,  # button
        "style": style if style is not None else _style_for(command),
        "label": (label or command)[:80],  # Discord caps button labels at 80
        "custom_id": f"{CUSTOM_ID_PREFIX}{command}"[:100],
    }


def actions_to_components(actions: object) -> Optional[list]:
    """Render neutral button descriptors as Discord ``components``, or None.

    ``actions`` is the list a caller already built with control.make_action
    (``[{"label", "command"}, ...]``). Returns Discord's
    ``[{"type": 1, "components": [button, ...]}]`` (one action row, capped at
    MAX_BUTTONS) — or None when the control loop is disabled, the list is empty,
    or nothing in it carries a command, so a push stays buttonless and
    byte-identical to before.
    """
    if not enabled() or not isinstance(actions, list):
        return None
    buttons: list[dict] = []
    for action in actions:
        command = _command_of(action)
        if not command:
            continue
        label = action.get("label") if isinstance(action, dict) else None
        buttons.append(make_button(str(label or command), command))
        if len(buttons) >= MAX_BUTTONS:
            break
    if not buttons:
        return None
    return [{"type": 1, "components": buttons}]  # one action row


# --- Inbound: channel polling (sweep side) -----------------------------------

def extract_commands(messages: object, since_id: Optional[str]) -> tuple[list[str], Optional[str]]:
    """Pure parse of a Discord messages payload into (commands, new_cursor).

    ``messages`` is the JSON list returned by ``GET /channels/{id}/messages``
    (newest-first). Returns the non-empty message contents in chronological
    (oldest-first) order so a batch of commands applies in the order it was
    sent, plus the newest message id seen (the cursor to persist). Only messages
    strictly newer than ``since_id`` are returned; malformed entries are skipped.
    The result is capped at MAX_PER_POLL.
    """
    if not isinstance(messages, list):
        return [], since_id

    since = int(since_id) if (isinstance(since_id, str) and since_id.isdigit()) else None
    rows: list[tuple[int, str]] = []  # (snowflake, content)
    newest = since
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        raw_id = msg.get("id")
        if not isinstance(raw_id, str) or not raw_id.isdigit():
            continue
        snowflake = int(raw_id)
        if since is not None and snowflake <= since:
            continue
        if newest is None or snowflake > newest:
            newest = snowflake
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            rows.append((snowflake, content.strip()))

    rows.sort(key=lambda r: r[0])  # oldest-first
    commands = [content for _, content in rows]
    if len(commands) > MAX_PER_POLL:
        log.warning("discord_control: %d commands polled; processing the first %d",
                    len(commands), MAX_PER_POLL)
        commands = commands[:MAX_PER_POLL]
    new_cursor = str(newest) if newest is not None else since_id
    return commands, new_cursor


def _get_messages(channel_id: str, after: Optional[str], timeout: float) -> object:
    """One REST GET against the control channel; returns the parsed JSON list."""
    params: dict[str, object] = {"limit": 1 if after is None else _FETCH_LIMIT}
    if after is not None:
        params["after"] = after
    resp = requests.get(
        f"{API_BASE}/channels/{channel_id}/messages",
        headers={
            "Authorization": f"Bot {_token()}",
            "User-Agent": "notify-watcher (https://github.com, 1.0)",
        },
        params=params,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def poll(state: dict, timeout: float = 15.0) -> list[str]:
    """Drain new control-channel messages into command strings; [] when off.

    The cursor (newest handled message id) lives in
    ``state["discord_control"]["last_id"]``. The very first poll has no cursor:
    it seeds the cursor to the channel's latest message id and returns [] WITHOUT
    dispatching, so an existing channel backlog is never replayed (Discord, unlike
    ntfy's ~12 h cache, keeps history forever). Network errors are logged and
    yield [] without advancing the cursor, so the batch is re-read next run.
    """
    if not enabled():
        return []
    ctl = state.setdefault("discord_control", {})
    last_id = ctl.get("last_id")
    try:
        payload = _get_messages(_control_channel(), last_id, timeout)
    except Exception as exc:  # noqa: BLE001 - control must never block the run
        log.warning("discord_control poll failed (will retry next run): %s", exc)
        return []

    if last_id is None:
        # First run: seed the cursor to the latest id, dispatch nothing.
        _, newest = extract_commands(payload, None)
        if newest:
            ctl["last_id"] = newest
            log.info("discord_control: seeded cursor at %s (backlog skipped)", newest)
        return []

    commands, newest = extract_commands(payload, last_id)
    if newest and newest != last_id:
        ctl["last_id"] = newest
    if commands:
        log.info("discord_control: polled %d command(s)", len(commands))
    return commands
