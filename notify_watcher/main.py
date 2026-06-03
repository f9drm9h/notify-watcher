"""Run every topic check, tolerate per-topic failures, save state once.

Each topic is a function `(state: dict) -> dict` that may push notifications
as a side effect and returns the (possibly updated) state dict. If a topic
raises, we log it and continue with the next topic so one broken source
never silences the others.
"""
from __future__ import annotations

import logging
import os
import sys
import traceback
from typing import Callable

from . import ntfy
from . import state as state_mod
from .topics import deals, games, ios_release, movies, visa_bulletin, wwdc

Topic = Callable[[dict], dict]

TOPICS: list[tuple[str, Topic]] = [
    ("visa_bulletin", visa_bulletin.run),
    ("wwdc", wwdc.run),
    ("ios_release", ios_release.run),
    ("movies", movies.run),
    ("games", games.run),
    ("deals", deals.run),
]


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    log = logging.getLogger("notify_watcher")

    # One-off delivery test: send a single notification and exit, so you can
    # confirm the ntfy pipeline reaches your phone without waiting for a real
    # source change. Triggered via the workflow_dispatch `test_push` input.
    if os.environ.get("NOTIFY_TEST_PUSH"):
        log.info("NOTIFY_TEST_PUSH set: sending test notification")
        ntfy.push(
            title="notify-watcher test",
            message="Test push - your notify-watcher pipeline is working.",
            tags="white_check_mark",
        )
        log.info("test push sent")
        return 0

    state = state_mod.load()

    for name, run in TOPICS:
        log.info("[%s] starting", name)
        try:
            state = run(state)
            log.info("[%s] ok", name)
        except Exception as exc:  # noqa: BLE001 - we deliberately swallow
            log.error("[%s] failed: %s", name, exc)
            log.debug("[%s] traceback:\n%s", name, traceback.format_exc())

    state_mod.save(state)
    # Always exit 0: a per-topic failure (e.g. transient network error) is
    # already logged above and must not turn the scheduled workflow red.
    return 0


if __name__ == "__main__":
    sys.exit(main())
