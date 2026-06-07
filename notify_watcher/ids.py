"""Compact, stable identifiers for dedup seen-lists.

A seen-list exists only to answer "have I handled this item before?" — it never
needs the original string back. Google News article ids are ~200 chars, and the
per-title seen-lists were by far the largest thing in state.json (which the
runner commits every few hours). Storing a short hash instead of the raw id
shrinks those lists ~10x while keeping dedup exact.

`short` is a pure function (BLAKE2s, 8-byte digest -> 16 hex chars), so the same
id always maps to the same token across runs and machines — dedup stays
deterministic. 16 hex chars (64 bits) is collision-safe for the few hundred ids
a topic ever holds.

`normalize_seen` migrates an existing list on load: tokens that are already
short hashes are kept, raw ids are hashed. This makes the switch seamless — the
first run after deploy hashes the stored raw ids, and a still-in-window article
hashes to that same token, so it is recognised as seen and NOT re-alerted.
"""
from __future__ import annotations

import hashlib

HASH_LEN = 16  # 8-byte BLAKE2s digest rendered as hex
_HEX = frozenset("0123456789abcdef")


def short(value: str) -> str:
    """Stable 16-hex-char hash of an id/URL. Pure and deterministic."""
    return hashlib.blake2s((value or "").encode("utf-8"), digest_size=8).hexdigest()


def _is_short(token: str) -> bool:
    """True if `token` is already one of our short hashes.

    A raw Google News id or article URL is far longer than 16 chars (or contains
    non-hex characters), so it can never be mistaken for an existing hash; that
    keeps `normalize_seen` safe and idempotent.
    """
    return len(token) == HASH_LEN and all(c in _HEX for c in token)


def normalize_seen(seen: list[str]) -> list[str]:
    """Return `seen` with every entry as a short hash (raw ids hashed once)."""
    return [t if _is_short(t) else short(t) for t in seen]
