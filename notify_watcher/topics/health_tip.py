"""The "health tip" content provider for the consolidated spark topic.

No longer a standalone TOPICS entry: topics/spark.py owns the 6-hour firing
gate and the quote -> fact -> health-tip rotation, and calls ``send_tip`` when
the rotation lands on a health tip. The tip comes from a curated, vetted
knowledge base (data/health_tips.json, sourced from CDC/WHO/MedlinePlus) - an
LLM is never allowed to invent medical advice. When a summary provider key is
set, the already-vetted tip is optionally *reworded* for variety via
notify_watcher.summarize; on any failure or absent key we send the tip verbatim,
so the feature degrades gracefully and needs no secrets.

Tip selection rotates by day-of-year (unchanged from the old daily topic), so
there are no repeats within a year for a KB of >=366 tips and an even spread
for smaller ones. The per-day guard (health_tip_last_sent) also stays: spark's
rotation lands on a health tip every ~18 hours, so two slots can fall on the
same calendar day - the second returns False (spark leaves its window
unstamped) and the tip fires on the next day's slot instead, keeping the old
"at most one health tip per day" contract.
"""
from __future__ import annotations

import datetime as _dt
import logging

from .. import events, kb, summarize

log = logging.getLogger(__name__)

STATE_KEY = "health_tip_last_sent"
TIPS_PATH = kb.DATA_DIR / "health_tips.json"

# Emits under the consolidated spark topic; spark.py owns the TOPICS entry.
TOPIC = "spark"

_REWORD_SYSTEM = (
    "You reword a single evidence-based health tip for a daily push "
    "notification. Preserve the meaning and any numbers EXACTLY; do not add new "
    "claims, advice, or facts. Reply with one plain-text sentence of at most "
    "~30 words: no preamble, no markdown, no quotation marks."
)


def _today() -> str:
    return _dt.date.today().isoformat()


def send_tip(state: dict) -> bool:
    """Send one vetted health tip under the spark topic.

    Called by topics/spark.py when its rotation lands on a health tip; the
    6-hour firing gate lives there. Returns True when a tip was emitted.
    """
    if state.get(STATE_KEY) == _today():
        log.info("health tip already sent today; retrying next window")
        return False

    # The health KB uses a "tip" field; selection is a shared day-of-year pick.
    tips = kb.load(TIPS_PATH, field="tip")
    chosen = kb.pick(tips)
    if not chosen:
        log.warning("no health tips available; skipping")
        return False
    tip, src = chosen["tip"], chosen.get("src", "")

    body = summarize.one_line(_REWORD_SYSTEM, tip) or tip
    if src:
        body = f"{body} (Source: {src})"

    events.emit(state, title="Health tip", body=body, topic=TOPIC,
                severity="low", source="Health", tags="apple",
                legacy_priority="low", legacy_action="push")
    log.info("sent health tip")
    state[STATE_KEY] = _today()
    return True
