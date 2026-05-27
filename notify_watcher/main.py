"""Run every topic check, tolerate per-topic failures, save state once.

Each topic is a function `(state: dict) -> dict` that may push notifications
as a side effect and returns the (possibly updated) state dict. If a topic
raises, we log it and continue with the next topic so one broken source
never silences the others.
"""
from __future__ import annotations

import logging
import sys
import traceback
from typing import Callable

from . import state as state_mod
from .topics import visa_bulletin, wwdc

Topic = Callable[[dict], dict]

TOPICS: list[tuple[str, Topic]] = [
    ("visa_bulletin", visa_bulletin.run),
    ("wwdc", wwdc.run),
]


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    log = logging.getLogger("notify_watcher")

    state = state_mod.load()
    any_failed = False

    for name, run in TOPICS:
        log.info("[%s] starting", name)
        try:
            state = run(state)
            log.info("[%s] ok", name)
        except Exception as exc:  # noqa: BLE001 - we deliberately swallow
            any_failed = True
            log.error("[%s] failed: %s", name, exc)
            log.debug("[%s] traceback:\n%s", name, traceback.format_exc())

    state_mod.save(state)
    # Exit 0 even on per-topic failure so the workflow stays green for
    # transient network errors. Logs show the failure for review.
    return 0 if not any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
