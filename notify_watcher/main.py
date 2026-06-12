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

from . import control, ntfy
from . import state as state_mod
from .topics import (
    air_quality,
    anthropic_news,
    apod,
    astronomy,
    baseball,
    beach_day,
    bills,
    blood_donation,
    deals,
    digest_topic,
    energy,
    energy_learn,
    fda,
    fuel,
    fx,
    games,
    golden_sun,
    groceries,
    habits,
    health_tip,
    holidays,
    ios_release,
    iss,
    itsc,
    launches,
    learn,
    marine,
    movies,
    music,
    onamet,
    outages,
    quakes,
    recap,
    reminders,
    soundcore_pro,
    spending,
    twitch,
    uv,
    visa_bulletin,
    watchdog,
    weather,
    wwdc,
    youtube,
)

Topic = Callable[[dict], dict]

# Daily-only topics (digest flush, health tip, learn, groceries, itsc, …)
# gate on NOTIFY_DAILY.
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
    # golden_sun merges wiki news + r/GoldenSun + Google News into the shared
    # news engine: remaster/official items push live, community posts buffer
    # to the digest, so it must run before digest_topic.
    ("golden_sun", golden_sun.run),
    # baseball checks Dominican player milestones (live push) every run and
    # adds the followed team's previous-day result to the digest on the daily
    # run, so it must run before digest_topic.
    ("baseball", baseball.run),
    # soundcore_pro discovers new Liberty Pro products and appends them to
    # state["auto_products"]; deals runs next so a same-run discovery is
    # price-tracked immediately.
    ("soundcore_pro", soundcore_pro.run),
    ("deals", deals.run),
    # groceries: weekly supermarket deals (La Sirena/Nacional/Bravo). A big
    # discount pushes; the rest buffer to the digest, so it must run before
    # digest_topic. Daily-only (NOTIFY_DAILY) — weekly pools don't change
    # run-to-run.
    ("groceries", groceries.run),
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
    # onamet: official DR severe-weather watches/warnings (INDOMET CAP feed);
    # every new alert pushes live. outages: scheduled power cuts for the watched
    # zones (EDEESTE weekly PDF; EDESUR page optional), pushed the day before
    # (or day-of when published late).
    ("onamet", onamet.run),
    ("outages", outages.run),
    ("quakes", quakes.run),
    ("air_quality", air_quality.run),
    ("fx", fx.run),
    # fuel: official DR weekly fuel prices (MICM notice PDF). A big week-over-
    # week move pushes; ordinary weeks buffer one line to the digest, so it
    # must run before digest_topic. Daily-only — prices change once a week.
    ("fuel", fuel.run),
    # Timely alerters (run every cycle): imminent launches, ISS passes, and new
    # official Anthropic posts. Each seeds silently and dedups.
    ("launches", launches.run),
    ("iss", iss.run),
    ("anthropic_news", anthropic_news.run),
    # youtube pushes once per new upload from each followed channel (free
    # per-channel Atom feed, no key); seeds silently and dedups by video id.
    ("youtube", youtube.run),
    # habit nudges (water, etc. from habits.json) fire on several daytime slots
    # across the 3-hourly grid, so this runs every cycle (not daily-only) and
    # dedups per slot per habit in state.
    ("habits", habits.run),
    ("digest", digest_topic.run),
    ("health_tip", health_tip.run),
    # learn's consolidated push is daily-only; its standalone knowledge push
    # fires every cycle (guarded per 3-hour window in state). reminders is
    # daily-only too. Independent of digest, so order among the daily-only
    # topics doesn't matter.
    ("learn", learn.run),
    # energy_learn is daily-only too: one calm educational "Today's spark" push.
    # Order doesn't matter among the daily-only topics; it reads the event_log
    # (populated by the collectors earlier this run) for its occasional news slot.
    ("energy_learn", energy_learn.run),
    ("reminders", reminders.run),
    # bills: monthly utility-bill due-date reminders (reminders.json -> bills).
    # Daily-only date math like reminders; pushes 5 days and 1 day before.
    ("bills", bills.run),
    # spending ingests BHD transaction emails every cycle (no-op without the
    # GMAIL_* secrets) and pushes a weekly summary on the first daily run of
    # each ISO week, like recap.
    ("spending", spending.run),
    # recap is weekly: on the first daily run of each ISO week it summarizes the
    # past week's event log + topic health into one Monday-morning push.
    ("recap", recap.run),
    ("blood_donation", blood_donation.run),
    # Daily-only "today" summaries: holiday heads-up, high-UV and rough-seas
    # alerts (threshold-gated), and the astronomy almanac.
    ("holidays", holidays.run),
    # itsc: academic-calendar deadlines from itsc.edu.do, pushed 7 days and
    # 1 day before each boundary. Daily-only, push-only (never digests).
    ("itsc", itsc.run),
    ("uv", uv.run),
    ("marine", marine.run),
    ("astronomy", astronomy.run),
    # apod is daily-only: NASA's Astronomy Picture of the Day, attached inline.
    ("apod", apod.run),
    # beach_day fires only on the configured weekdays (default Saturday): one
    # 0-10 "is today a beach day?" score from waves + rain + UV + temperature.
    ("beach_day", beach_day.run),
    # watchdog runs LAST: it reads the topic_health entries this loop stamped for
    # every topic above and pushes once when one has been failing for 48h+ — so a
    # dead feed can't go silently unnoticed. Pure state inspection, no network.
    ("watchdog", watchdog.run),
]


def _selected_topics(only: str) -> list[tuple[str, Topic]]:
    """Filter TOPICS to a comma-separated allowlist (``NOTIFY_ONLY``), preserving
    order. Empty/blank means all topics. Unknown names are simply ignored. This lets
    a lightweight workflow run a single fast topic often — e.g. a 15-minute Twitch
    'went live' check — without invoking the full 3-hourly sweep."""
    wanted = {t.strip() for t in only.split(",") if t.strip()}
    if not wanted:
        return TOPICS
    return [(name, run) for name, run in TOPICS if name in wanted]


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

    # Reply-button control channel: poll + dispatch BEFORE the topic loop so a
    # command takes effect in the same run that reads it, and regardless of
    # NOTIFY_ONLY so the frequent twitch run keeps command latency low. A no-op
    # when NTFY_CONTROL_TOPIC is unset; a failure must never block the run.
    try:
        control.dispatch(control.poll(state), state)
        # Due "remind later" re-fires and queued "show more" pushes ride the
        # same cadence as the poll (every run, incl. the 15-min twitch runs).
        control.process_pending(state)
    except Exception as exc:  # noqa: BLE001 - control errors are never fatal
        log.error("control channel failed: %s", exc)

    # Enable the daily-only topics on the first run past DAILY_UTC_HOUR. Setting
    # the env var keeps each daily topic's existing NOTIFY_DAILY check unchanged.
    if _is_daily_run():
        os.environ["NOTIFY_DAILY"] = "1"
        log.info("daily run active: digest flush, health tip, and learn enabled")

    # Per-topic run telemetry for the dashboard. priority.decide/emit never see a
    # topic that THREW (it never emits), so the only place that knows a topic failed
    # is this loop. Stamp last-ok / last-error here; the dashboard turns it into the
    # "topic health / last successful run / failures" panels (docs/design/02-dashboard).
    health: dict = state.setdefault("topic_health", {})
    run_ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
    ok_count = fail_count = 0

    topics = _selected_topics(os.environ.get("NOTIFY_ONLY", ""))
    if len(topics) != len(TOPICS):
        log.info("NOTIFY_ONLY active: running %d of %d topics", len(topics), len(TOPICS))

    for name, run in topics:
        log.info("[%s] starting", name)
        entry = health.setdefault(name, {})
        try:
            state = run(state)
            entry["last_ok"] = run_ts
            entry.pop("last_error", None)
            entry.pop("last_error_ts", None)
            ok_count += 1
            log.info("[%s] ok", name)
        except Exception as exc:  # noqa: BLE001 - we deliberately swallow
            entry["last_error"] = str(exc)
            entry["last_error_ts"] = run_ts
            fail_count += 1
            log.error("[%s] failed: %s", name, exc)
            log.debug("[%s] traceback:\n%s", name, traceback.format_exc())

    state["last_run"] = {"ts": run_ts, "ok": ok_count, "failed": fail_count}
    state_mod.save(state)
    # Always exit 0: a per-topic failure (e.g. transient network error) is
    # already logged above and must not turn the scheduled workflow red.
    return 0


if __name__ == "__main__":
    sys.exit(main())
