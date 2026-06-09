"""Run every topic check, tolerate per-topic failures, save state once.

Each topic is a function `(state: dict) -> dict` that may push notifications
as a side effect and returns the (possibly updated) state dict. If a topic
raises, we log it and continue with the next topic so one broken source
never silences the others.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import sys
import traceback
from typing import Callable

from . import ntfy
from . import state as state_mod
from .topics import (
    air_quality,
    anthropic_news,
    astronomy,
    blood_donation,
    deals,
    digest_topic,
    energy,
    fda,
    fx,
    games,
    habits,
    health_tip,
    holidays,
    ios_release,
    iss,
    launches,
    learn,
    marine,
    movies,
    music,
    quakes,
    reminders,
    soundcore_pro,
    twitch,
    uv,
    visa_bulletin,
    weather,
    wwdc,
)

Topic = Callable[[dict], dict]

# Daily-only topics (digest flush, health tip, learn) gate on NOTIFY_DAILY.
# Rather than rely on a dedicated cron firing (GitHub Actions routinely delays
# and silently DROPS scheduled runs — a schedule sitting minutes off the main
# grid is especially prone to being skipped, which is why these never ran), we
# decide "is this the daily run?" here: the first invocation on/after this UTC
# hour each day does the daily work. Each daily topic still has its own
# per-day, set-on-success guard (health_tip_last_sent, etc.), so triggering on
# every post-threshold run is idempotent and a same-day failure naturally
# retries on the next 3-hourly run.
DAILY_UTC_HOUR = 12


def _is_daily_run() -> bool:
    """True if the daily-only topics should run on this invocation.

    Honors an explicit NOTIFY_DAILY from the workflow (manual `daily=true`
    dispatch) and otherwise fires once the UTC hour has reached DAILY_UTC_HOUR.
    """
    if os.environ.get("NOTIFY_DAILY"):
        return True
    return _dt.datetime.now(_dt.timezone.utc).hour >= DAILY_UTC_HOUR

TOPICS: list[tuple[str, Topic]] = [
    ("visa_bulletin", visa_bulletin.run),
    ("wwdc", wwdc.run),
    ("ios_release", ios_release.run),
    ("movies", movies.run),
    # games is weekly: it self-gates to the first daily run of each ISO week
    # (see games.run), batching release-date + news updates into one catch-up.
    ("games", games.run),
    # twitch pings once per live session; music watches followed artists every
    # run and adds one library-seeded discovery pick on the daily run.
    ("twitch", twitch.run),
    ("music", music.run),
    # soundcore_pro discovers new Liberty Pro products and appends them to
    # state["auto_products"]; deals runs next so a same-run discovery is
    # price-tracked immediately.
    ("soundcore_pro", soundcore_pro.run),
    ("deals", deals.run),
    # Domain monitors: collectors score items and either push live (high/
    # breakthrough) or buffer moderate ones. digest_topic must run AFTER the
    # collectors so same-day items are flushed; both digest_topic and health_tip
    # act only on the daily run (NOTIFY_DAILY).
    ("fda", fda.run),
    ("energy", energy.run),
    # Safety + life monitors. weather/quakes are geo-aware (live for a near, real
    # threat; smaller/region-relevant items buffer to the digest). air_quality and
    # fx are threshold alerters. All run before digest so same-run items flush.
    ("weather", weather.run),
    ("quakes", quakes.run),
    ("air_quality", air_quality.run),
    ("fx", fx.run),
    # Timely alerters (run every cycle): imminent launches, ISS passes, and new
    # official Anthropic posts. Each seeds silently and dedups.
    ("launches", launches.run),
    ("iss", iss.run),
    ("anthropic_news", anthropic_news.run),
    # habit nudges (water, etc. from habits.json) fire on several daytime slots
    # across the 3-hourly grid, so this runs every cycle (not daily-only) and
    # dedups per slot per habit in state.
    ("habits", habits.run),
    ("digest", digest_topic.run),
    ("health_tip", health_tip.run),
    # learn and reminders are daily-only too. Independent of digest, so order
    # among the daily-only topics doesn't matter.
    ("learn", learn.run),
    ("reminders", reminders.run),
    ("blood_donation", blood_donation.run),
    # Daily-only "today" summaries: holiday heads-up, high-UV and rough-seas
    # alerts (threshold-gated), and the astronomy almanac.
    ("holidays", holidays.run),
    ("uv", uv.run),
    ("marine", marine.run),
    ("astronomy", astronomy.run),
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

    # Enable the daily-only topics on the first run past DAILY_UTC_HOUR. Setting
    # the env var keeps each daily topic's existing NOTIFY_DAILY check unchanged.
    if _is_daily_run():
        os.environ["NOTIFY_DAILY"] = "1"
        log.info("daily run active: digest flush, health tip, and learn enabled")

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
