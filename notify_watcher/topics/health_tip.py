"""Topic: one evidence-based health tip per day.

Sends a single push from a curated, vetted knowledge base (data/health_tips.json,
sourced from CDC/WHO/MedlinePlus). The fact always comes from the vetted KB - an
LLM is never allowed to invent medical advice. When a summary provider key is
set, the already-vetted tip is optionally *reworded* for variety via
notify_watcher.summarize; on any failure or absent key we send the tip verbatim,
so the feature degrades gracefully and needs no secrets.

Daily-only: this topic acts only when NOTIFY_DAILY is set (the daily cron) and
is further guarded by health_tip_last_sent so a duplicate or drifted run never
double-sends. Tip selection rotates by day-of-year, so there are no repeats
within a year for a KB of >=366 tips and an even spread for smaller ones.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os

from .. import kb, ntfy, summarize

log = logging.getLogger(__name__)

STATE_KEY = "health_tip_last_sent"
TIPS_PATH = kb.DATA_DIR / "health_tips.json"

_REWORD_SYSTEM = (
    "You reword a single evidence-based health tip for a daily push "
    "notification. Preserve the meaning and any numbers EXACTLY; do not add new "
    "claims, advice, or facts. Reply with one plain-text sentence of at most "
    "~30 words: no preamble, no markdown, no quotation marks."
)


def _today() -> str:
    return _dt.date.today().isoformat()


def run(state: dict) -> dict:
    if not os.environ.get("NOTIFY_DAILY"):
        return state  # only the daily cron run sends a tip
    if state.get(STATE_KEY) == _today():
        log.info("health tip already sent today; skipping")
        return state

    # The health KB uses a "tip" field; selection is a shared day-of-year pick.
    tips = kb.load(TIPS_PATH, field="tip")
    chosen = kb.pick(tips)
    if not chosen:
        log.warning("no health tips available; skipping")
        return state
    tip, src = chosen["tip"], chosen.get("src", "")

    body = summarize.one_line(_REWORD_SYSTEM, tip) or tip
    if src:
        body = f"{body} (Source: {src})"

    ntfy.push(title="Health tip", message=body, tags="apple", priority="low")
    log.info("sent daily health tip")
    state[STATE_KEY] = _today()
    return state
