"""Daily digest buffer for moderate-importance monitor items.

Collectors run every few hours and route moderate-tier items here instead of
pushing them live, so routine news never causes alert fatigue. Once a day the
digest topic flushes the buffer into a single grouped notification and clears
it. The buffer is a capped list inside state.json, so it can never grow without
bound and is emptied every flush.

State keys owned by this module:
  digest_buffer    : list[dict]  pending items {title, url, source, tier, score}
  digest_last_sent : str         YYYY-MM-DD guard so a day is flushed once
"""
from __future__ import annotations

import datetime as _dt
import logging

from . import ntfy

log = logging.getLogger(__name__)

BUFFER_KEY = "digest_buffer"
LAST_SENT_KEY = "digest_last_sent"
_DEFAULT_MAX_BUFFER = 50
_DEFAULT_MAX_IN_MSG = 25


def _today() -> str:
    return _dt.date.today().isoformat()


def add(state: dict, item: dict, cfg: dict) -> None:
    """Append a moderate item to the buffer, keeping only the newest N.

    `item` is {title, url, source, tier, score}. The cap (digest.max_buffer)
    bounds state.json growth; when full the oldest pending item is dropped.
    """
    buf: list = state.setdefault(BUFFER_KEY, [])
    buf.append({
        "title": item.get("title", ""),
        "url": item.get("url", ""),
        "source": item.get("source", ""),
    })
    cap = int(cfg.get("max_buffer", _DEFAULT_MAX_BUFFER))
    if len(buf) > cap:
        del buf[:-cap]


def flush(state: dict, cfg: dict) -> bool:
    """Send one grouped digest push and clear the buffer. Returns True if sent.

    Idempotent per day via digest_last_sent: a second flush on the same date is
    a no-op, so a duplicate or drifted daily run never double-sends. An empty
    buffer is also a no-op (and does not consume the day's stamp).
    """
    if state.get(LAST_SENT_KEY) == _today():
        log.info("digest already sent today; skipping")
        return False

    buf: list = state.get(BUFFER_KEY) or []
    if not buf:
        log.info("digest buffer empty; nothing to send")
        return False

    max_in_msg = int(cfg.get("max_items_in_message", _DEFAULT_MAX_IN_MSG))
    shown = buf[:max_in_msg]
    overflow = len(buf) - len(shown)

    # Group by source for a scannable body.
    by_source: dict[str, list[str]] = {}
    for it in shown:
        by_source.setdefault(it.get("source", "Other"), []).append(it.get("title", ""))

    lines: list[str] = []
    for source, titles in by_source.items():
        lines.append(source.upper())
        lines.extend(f"  - {t}" for t in titles)
    if overflow > 0:
        lines.append(f"(+{overflow} more)")

    ntfy.push(
        title=f"Daily digest - {len(buf)} update(s)",
        message="\n".join(lines),
        tags="clipboard",
        priority="default",
    )
    log.info("sent daily digest with %d item(s)", len(buf))

    state[BUFFER_KEY] = []
    state[LAST_SENT_KEY] = _today()
    return True
