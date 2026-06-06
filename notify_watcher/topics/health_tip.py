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
import json
import logging
import os
from pathlib import Path

from .. import ntfy, summarize

log = logging.getLogger(__name__)

STATE_KEY = "health_tip_last_sent"
TIPS_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "health_tips.json"

_REWORD_SYSTEM = (
    "You reword a single evidence-based health tip for a daily push "
    "notification. Preserve the meaning and any numbers EXACTLY; do not add new "
    "claims, advice, or facts. Reply with one plain-text sentence of at most "
    "~30 words: no preamble, no markdown, no quotation marks."
)


def _today() -> str:
    return _dt.date.today().isoformat()


def _load_tips() -> list[dict]:
    try:
        data = json.loads(TIPS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("could not load health tips: %s", exc)
        return []
    return [t for t in data if isinstance(t, dict) and t.get("tip")]


def run(state: dict) -> dict:
    if not os.environ.get("NOTIFY_DAILY"):
        return state  # only the daily cron run sends a tip
    if state.get(STATE_KEY) == _today():
        log.info("health tip already sent today; skipping")
        return state

    tips = _load_tips()
    if not tips:
        log.warning("no health tips available; skipping")
        return state

    # Rotate by day-of-year for an even, repeat-resistant spread.
    idx = _dt.date.today().timetuple().tm_yday % len(tips)
    chosen = tips[idx]
    tip, src = chosen["tip"], chosen.get("src", "")

    body = summarize.one_line(_REWORD_SYSTEM, tip) or tip
    if src:
        body = f"{body} (Source: {src})"

    ntfy.push(title="Health tip", message=body, tags="apple", priority="low")
    log.info("sent daily health tip (idx %d)", idx)
    state[STATE_KEY] = _today()
    return state
