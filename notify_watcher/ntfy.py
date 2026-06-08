"""Send a push notification via ntfy.sh.

Reads NTFY_TOPIC (required) and NTFY_SERVER (optional, defaults to
https://ntfy.sh) from environment variables. Nothing is hardcoded so this
file is safe to commit to a public repo.
"""
from __future__ import annotations

import base64
import os
from typing import Optional

import requests

DEFAULT_SERVER = "https://ntfy.sh"


class NtfyConfigError(RuntimeError):
    pass


def _encode_header(value: str) -> str:
    """Make a header value safe for ntfy without mangling non-ASCII text.

    HTTP/ntfy header values must be ASCII (requests encodes them latin-1). A pure
    ASCII title passes through unchanged. Anything with accents/emoji is wrapped
    as a single RFC 2047 base64 encoded-word (``=?UTF-8?B?...?=``), which ntfy
    decodes back to UTF-8 — so "Café" renders as "Café" instead of the "CafÃ©"
    you got from the old utf-8-bytes-as-latin-1 reinterpretation.
    """
    try:
        value.encode("ascii")
        return value
    except UnicodeEncodeError:
        b64 = base64.b64encode(value.encode("utf-8")).decode("ascii")
        return f"=?UTF-8?B?{b64}?="


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
    # ntfy header values must be ASCII; RFC 2047-encode non-ASCII titles so
    # accented characters survive instead of being mojibake'd through latin-1.
    headers["Title"] = _encode_header(title)
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
