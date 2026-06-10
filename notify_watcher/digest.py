"""Daily digest buffer for moderate-importance monitor items.

Collectors run every few hours and route moderate-tier items here instead of
pushing them live, so routine news never causes alert fatigue. Once a day the
digest topic flushes the buffer into a single grouped notification and clears
it. The buffer is a capped list inside state.json, so it can never grow without
bound and is emptied every flush.

State keys owned by this module:
  digest_buffer    : list[dict]  pending items {title, url, source, score}
  digest_last_sent : str         YYYY-MM-DD guard so a day is flushed once
"""
from __future__ import annotations

import datetime as _dt
import logging

from . import ntfy

log = logging.getLogger(__name__)

BUFFER_KEY = "digest_buffer"
LAST_SENT_KEY = "digest_last_sent"
_DEFAULT_MAX_BUFFER = 120
_DEFAULT_MAX_IN_MSG = 30
_DEFAULT_MAX_PER_SOURCE = 8
# A digested item may carry an optional one-line `detail` (the event body) so
# topics whose info lives in the body — holidays, reminders, fx — survive being
# digested instead of pushed. Bounded so a long body can't blow up the message.
_MAX_DETAIL = 160


def _today() -> str:
    return _dt.date.today().isoformat()


def _drop_lowest(buf: list, candidates=None) -> None:
    """Remove the lowest-score buffered item, breaking ties toward the oldest.

    `candidates` restricts the choice to those buffer indices (used by the
    per-source cap); when None the whole buffer is considered (the global cap).
    min() returns the first index achieving the minimum, i.e. the oldest among
    equal-low scores, so a newer item of the same importance is kept over an
    older one.
    """
    pool = candidates if candidates is not None else range(len(buf))
    victim = min(pool, key=lambda i: buf[i].get("score", 0))
    del buf[victim]


def add(state: dict, item: dict, cfg: dict) -> None:
    """Append a moderate item, evicting by importance so low-volume topics survive.

    `item` is {title, url, source, score}; a caller that omits score defaults to
    0. Two caps keep the buffer both bounded and FAIR, replacing the old plain
    newest-N trim that let a high-volume source (a hot film/game) flood the
    buffer and evict the low-volume domain monitors (FDA, energy) before the
    once-a-day flush ever ran:

      * max_per_source: no single source may hold more than this many items; when
        adding one would exceed it, that source's lowest-score item is dropped,
        so a chatty source can't monopolize the buffer and starve quieter ones.
      * max_buffer: the global ceiling; when exceeded, the lowest-score item
        across the whole buffer is dropped.

    Both evictions drop by score (oldest on a tie) and the flush ranks by score,
    so the most important pending items always survive and surface first.
    """
    buf: list = state.setdefault(BUFFER_KEY, [])
    src = item.get("source", "")
    buf.append({
        "title": item.get("title", ""),
        "url": item.get("url", ""),
        "source": src,
        "score": int(item.get("score", 0) or 0),
        "detail": (item.get("detail") or "")[:_MAX_DETAIL],
    })

    per_source = int(cfg.get("max_per_source", _DEFAULT_MAX_PER_SOURCE))
    same = [i for i, it in enumerate(buf) if it.get("source") == src]
    if len(same) > per_source:
        _drop_lowest(buf, same)

    cap = int(cfg.get("max_buffer", _DEFAULT_MAX_BUFFER))
    while len(buf) > cap:
        _drop_lowest(buf)


def flush(state: dict, cfg: dict, header: str | None = None) -> bool:
    """Send one grouped digest push and clear the buffer. Returns True if sent.

    Idempotent per day via digest_last_sent: a second flush on the same date is
    a no-op, so a duplicate or drifted daily run never double-sends. An empty
    buffer is also a no-op (and does not consume the day's stamp).

    `header`, when given, becomes the first line of the message (the digest
    topic passes the morning weather one-liner here).
    """
    if state.get(LAST_SENT_KEY) == _today():
        log.info("digest already sent today; skipping")
        return False

    buf: list = state.get(BUFFER_KEY) or []
    if not buf:
        log.info("digest buffer empty; nothing to send")
        return False

    max_in_msg = int(cfg.get("max_items_in_message", _DEFAULT_MAX_IN_MSG))

    # Rank by importance so the most significant items survive truncation and
    # surface first. Overflow now drops the LEAST important items; previously
    # `buf[:max_in_msg]` kept the oldest and silently dropped the newest. Sort is
    # stable, so equal-score items keep buffer (chronological) order.
    ranked = sorted(buf, key=lambda it: it.get("score", 0), reverse=True)
    shown = ranked[:max_in_msg]
    overflow = len(buf) - len(shown)

    # Group by source for a scannable body, with sources ordered by their best
    # item's score and items within a group already in score order (from shown).
    by_source: dict[str, list[dict]] = {}
    for it in shown:
        by_source.setdefault(it.get("source", "Other"), []).append(it)
    ordered_sources = sorted(
        by_source,
        key=lambda s: max(it.get("score", 0) for it in by_source[s]),
        reverse=True,
    )

    lines: list[str] = []
    if header:
        lines.append(header)
    for source in ordered_sources:
        lines.append(source.upper())
        for it in by_source[source]:
            title = it.get("title", "")
            detail = it.get("detail", "")
            # Title-complete items (collector/news headlines) carry no detail and
            # render as before; body-informative ones append their detail so a
            # generic title ("Reminder") still conveys what happened.
            if detail:
                line = f"{title} - {detail}" if title else detail
            else:
                line = title
            lines.append(f"  - {line}")
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
