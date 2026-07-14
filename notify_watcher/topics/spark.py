"""Topic: one consolidated "spark" push every 6 hours, rotating content types.

Replaces the three separate educational channels (the wikiquote story, learn's
3-hourly knowledge story, and the daily health tip) with a single topic that
fires once per 6-hour window and rotates the content type in a fixed sequence:

    quote  -> wikiquote.send_quote      a real Wikiquote quote from a curated
                                        figure, wrapped in a short Gemini story
    fact   -> learn.send_knowledge      a short Gemini knowledge story from
                                        data/knowledge_topics.json
    health -> health_tip.send_tip       one vetted tip from data/health_tips.json

Each provider keeps its own dedup/rotation memory exactly as before (the
curated-figure category rotation + 30-day no-repeat, the seen-topic dedup, the
day-of-year tip pick + per-day guard); this module only owns the firing gate,
the type rotation, and the single delivery topic ("spark", so everything lands
in one Discord channel).

Design choices:

  * 6-hour epoch window. The double-send guard is the same window math the
    watch workflow applies for its 3-hour full-sweep gate
    (``time.time() // window_secs``), just with a 21600s window: the first
    surviving run in each window sends, re-runs inside it are no-ops.
  * Rotation advances only on success. A failed send (source down, no LLM
    provider, tip already sent today) leaves both the window unstamped and the
    rotation pointer unchanged, so the next run retries the same type — a
    transient failure never skips a content type.
  * Standalone, not daily-gated. Like the channels it replaces (except the old
    health_tip, which rode the daily run and now rotates in every ~18h).
  * Health contract. spark is in health.ADOPTED: the quote and fact providers
    report source_ok / source_failed under this topic. The health-tip leg is
    local vetted KB only, so it makes no claim, matching the old health_tip
    topic which was deliberately not adopted.
"""
from __future__ import annotations

import logging
import time

from . import health_tip, learn, wikiquote

log = logging.getLogger(__name__)

TOPIC = "spark"

SPARK_SENT_KEY = "spark_last_sent"   # int: the 6-hour epoch window last sent
SPARK_KIND_KEY = "spark_next_kind"   # the content type the next firing sends
SPARK_WINDOW_SECS = 6 * 3600         # 21600s; the full sweep gates on 10800s

SEQUENCE = ("quote", "fact", "health")


def _window(now_ts: float | None = None) -> int:
    """The 6-hour epoch window id (same pattern as the full-sweep gate)."""
    return int((time.time() if now_ts is None else now_ts) // SPARK_WINDOW_SECS)


def _next_kind(state: dict) -> str:
    """The content type this firing should send (unknown/missing -> first)."""
    kind = str(state.get(SPARK_KIND_KEY, ""))
    return kind if kind in SEQUENCE else SEQUENCE[0]


def _send(kind: str, state: dict) -> bool:
    """Dispatch one content type to its provider; True when it emitted."""
    # Looked up per call (not a module-level table) so tests patching the
    # provider functions on their modules are seen.
    if kind == "quote":
        return wikiquote.send_quote(state)
    if kind == "fact":
        return learn.send_knowledge(state)
    return health_tip.send_tip(state)


def _run(state: dict, now_ts: float | None = None) -> dict:
    window = _window(now_ts)
    if state.get(SPARK_SENT_KEY) == window:
        log.info("spark push already sent this 6-hour window; skipping")
        return state

    kind = _next_kind(state)
    if not _send(kind, state):
        # Window unstamped and pointer unchanged: the next run retries `kind`.
        log.info("spark %s send did not fire; retrying next run", kind)
        return state

    state[SPARK_SENT_KEY] = window
    state[SPARK_KIND_KEY] = SEQUENCE[(SEQUENCE.index(kind) + 1) % len(SEQUENCE)]
    log.info("sent spark %s (window %d); next type: %s",
             kind, window, state[SPARK_KIND_KEY])
    return state


def run(state: dict) -> dict:
    """Topic entry point: one rotating spark push per 6-hour window."""
    return _run(state)
