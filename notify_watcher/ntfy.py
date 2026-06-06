"""Send a push notification via ntfy.sh.

Reads NTFY_TOPIC (required) and NTFY_SERVER (optional, defaults to
https://ntfy.sh) from environment variables. Nothing is hardcoded so this
file is safe to commit to a public repo.
"""
from __future__ import annotations

import os
from typing import Optional

import requests

DEFAULT_SERVER = "https://ntfy.sh"


class NtfyConfigError(RuntimeError):
    pass


def _config() -> tuple[str, str]:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        raise NtfyConfigError(
            "NTFY_TOPIC environment variable is not set. "
            "Set it to your private ntfy topic name."
        )
    server = os.environ.get("NTFY_SERVER", "").strip() or DEFAULT_SERVER
    return server.rstrip("/"), topic


def push(
    title: str,
    message: str,
    click_url: Optional[str] = None,
    tags: Optional[str] = None,
    priority: Optional[str] = None,
    timeout: float = 15.0,
) -> None:
    """POST a notification to the configured ntfy topic.

    `priority` is an optional ntfy priority name ("min", "low", "default",
    "high", "urgent"); when None the server applies its default, so existing
    callers are unaffected. Used by the scored domain monitors to make
    breakthrough/high-tier alerts ring louder than routine ones.

    Raises requests.HTTPError on a non-2xx response so callers can decide
    whether to retry or log-and-continue.
    """
    server, topic = _config()
    url = f"{server}/{topic}"

    headers: dict[str, str] = {}
    # ntfy header values must be latin-1; encode non-ASCII so we never 400.
    headers["Title"] = title.encode("utf-8", "replace").decode("latin-1", "replace")
    if click_url:
        headers["Click"] = click_url
    if tags:
        headers["Tags"] = tags
    if priority:
        headers["Priority"] = priority

    resp = requests.post(
        url,
        data=message.encode("utf-8"),
        headers=headers,
        timeout=timeout,
    )
    resp.raise_for_status()
