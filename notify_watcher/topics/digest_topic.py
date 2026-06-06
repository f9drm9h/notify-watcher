"""Topic: flush the daily digest of moderate-importance monitor items.

Runs only on the daily cron (NOTIFY_DAILY set). Collectors accumulate moderate
items into state["digest_buffer"] throughout the day; this topic drains that
buffer into a single grouped notification and clears it. Registered AFTER the
collectors in main.py so items found on the same daily run are included.

All the real work (idempotent per-day guard, grouping, buffer clearing) lives
in notify_watcher.digest; this topic is just the scheduled entry point.
"""
from __future__ import annotations

import logging
import os

from .. import config, digest

log = logging.getLogger(__name__)


def run(state: dict) -> dict:
    if not os.environ.get("NOTIFY_DAILY"):
        return state  # only the daily cron run flushes the digest
    digest.flush(state, config.section("digest"))
    return state
