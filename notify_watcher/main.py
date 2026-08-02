"""Run every topic check, tolerate per-topic failures, save state once.

Each topic is a function `(state: dict) -> dict` that may push notifications
as a side effect and returns the (possibly updated) state dict. If a topic
raises, we log it and continue with the next topic so one broken source
never silences the others.

Health stamping (state["topic_health"]) has two regimes:

  - Legacy topics: "didn't raise" counts as success and stamps ``last_ok``.
  - Topics in health.ADOPTED report their source outcome explicitly via the
    topic health contract (health.source_ok / health.source_failed):
    ``last_ok`` is stamped only for a true ok report, a soft (swallowed)
    source failure is recorded as ``last_error`` + ``source_failed`` without
    refreshing ``last_ok``, and a run with no report leaves the entry
    untouched — so a soft failure stays sticky until a true success and the
    watchdog's no-successful-run check can finally see a dead source.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import sys
import traceback
from typing import Callable

from . import config, control, discord_control, health, ntfy
from . import state as state_mod
from .topics import (
    air_quality,
    anthropic_news,
    apod,
    astronomy,
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
    holidays,
    iss,
    itsc,
    launches,
    learn,
    life_dashboard,
    marine,
    music,
    onamet,
    outages,
    quakes,
    recap,
    reminders,
    research,
    soundcore_pro,
    spark,
    spending,
    twitch,
    uv,
    visa_bulletin,
    watchdog,
    weather,
    youtube,
)

Topic = Callable[[dict], dict]

# Daily-only topics (digest flush, learn, groceries, itsc, …) gate on
# NOTIFY_DAILY.
# Rather than rely on a dedicated cron firing (GitHub Actions routinely delays
# and silently DROPS scheduled runs — a schedule sitting minutes off the main
# grid is especially prone to being skipped, which is why these never ran), we
# decide "is this the daily run?" here: the first invocation on/after this UTC
# hour each day does the daily work. Each daily topic still has its own
# per-day, set-on-success guard (learn_last_sent, etc.), so triggering on
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
    # research is the on-demand /research article summary: a no-op unless the
    # dispatch carried NOTIFY_RESEARCH_URL, so it costs nothing on schedule.
    ("research", research.run),
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
    # breakthrough) or buffer moderate ones. The digest flush runs near the end
    # of TOPICS so every producer below gets a chance to add same-run items.
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
    # habit tracker (habits.json): local-time reminder slots pushed to the
    # habits channel with a ✅ reaction ack that logs completions in state.
    # Rides the 15-minute fast lane (watch.yml runs NOTIFY_ONLY=twitch,habits
    # between full sweeps) so minute-level slots fire on time; dedups per slot
    # per habit in state.
    ("habits", habits.run),
    # learn's consolidated push is daily-only. reminders is daily-only too.
    # Independent of digest, so order among the daily-only topics doesn't
    # matter. (learn's old 3-hourly knowledge push now rides spark below.)
    ("learn", learn.run),
    # spark consolidates the old wikiquote, knowledge-story and health-tip
    # channels: one push per 6-hour window (epoch-window guard in state),
    # rotating quote -> fact -> health tip. Standalone, not daily-gated.
    ("spark", spark.run),
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
    # life_dashboard is weekly too: on Sunday it stitches the week's event log,
    # topic health, fx state and bills into one themed life-digest push. Runs
    # late so it reads the same accumulated history recap does.
    ("life_dashboard", life_dashboard.run),
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
    # digest flushes after all digest-producing topics so daily-only items
    # created in this same run are not stranded until tomorrow. Watchdog still
    # stays last because it reads the health stamps produced by every topic.
    ("digest", digest_topic.run),
    # watchdog runs LAST: it reads the topic_health entries this loop stamped for
    # every topic above and pushes once when one has been failing for 48h+ — so a
    # dead feed can't go silently unnoticed. Pure state inspection, no network.
    ("watchdog", watchdog.run),
]


def _record_outcome(entry: dict, status: dict | None, *, adopted: bool,
                    run_ts: str) -> bool:
    """Stamp one topic's ``topic_health`` entry from its run outcome.

    ``status`` is the topic's health report (health.consume), or None when the
    topic made no claim this run. Returns True when the run counts as ok in the
    last_run telemetry. The rules (see module docstring): an explicit ok report
    — or, for legacy topics, simply not raising — stamps ``last_ok`` and clears
    errors; a source_failed report records a soft failure WITHOUT stamping
    ``last_ok``; an adopted topic with no report (gated daily topic on a
    3-hourly run, nothing configured) leaves the entry untouched so a prior
    soft failure stays sticky until a true success.
    """
    if status is None:
        if adopted:
            return True
        entry["last_ok"] = run_ts
        entry.pop("last_error", None)
        entry.pop("last_error_ts", None)
        return True
    if status.get("ok"):
        entry["last_ok"] = run_ts
        entry["last_data_count"] = int(status.get("data_count") or 0)
        entry.pop("last_error", None)
        entry.pop("last_error_ts", None)
        entry.pop("source_failed", None)
        return True
    entry["last_error"] = status.get("message") or "source failed"
    entry["last_error_ts"] = run_ts
    entry["source_failed"] = True
    return False


def _selected_topics(only: str) -> list[tuple[str, Topic]]:
    """Filter TOPICS to a comma-separated allowlist (``NOTIFY_ONLY``), preserving
    order. Empty/blank means all topics. Unknown names are simply ignored. This lets
    a lightweight workflow run a single fast topic often — e.g. a 15-minute Twitch
    'went live' check — without invoking the full 3-hourly sweep."""
    wanted = {t.strip() for t in only.split(",") if t.strip()}
    if not wanted:
        return TOPICS
    return [(name, run) for name, run in TOPICS if name in wanted]


def _is_topic_dispatch_run() -> bool:
    """True for /run-style manual single-topic dispatches.

    Twitch-only cadence runs also set NOTIFY_ONLY, but they are the normal
    low-latency control lane. Since the Cloudflare Worker cron replaced the
    GitHub schedule trigger, those cadence runs arrive as workflow_dispatch
    too — the workflow marks them with NOTIFY_SCHEDULED=1. Only an
    unscheduled workflow_dispatch + NOTIFY_ONLY is the on-demand path that
    should avoid flushing unrelated pending/control work.
    """
    return (
        os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
        and not os.environ.get("NOTIFY_SCHEDULED", "").strip()
        and bool(os.environ.get("NOTIFY_ONLY", "").strip())
    )


def _force_pending_flush() -> bool:
    """True when NOTIFY_PROCESS_PENDING explicitly opts an on-demand run back in
    to the control/pending work that :func:`_is_topic_dispatch_run` otherwise
    skips.

    The skip exists so a manual ``/run topic:x`` can't surprise-fire old LATER/
    MORE pushes; this is the deliberate override for the rare run where flushing
    those *is* what you want. It only matters on a topic-dispatch run — scheduled
    and full manual runs already process pending — so setting it elsewhere is a
    harmless no-op.
    """
    return bool(os.environ.get("NOTIFY_PROCESS_PENDING", "").strip())


def _run_counting_pushes(run: Topic, state: dict) -> tuple[dict, int]:
    """Run one topic while counting push attempts through the shared transport."""
    sent = 0
    original_push = ntfy.push

    def counted_push(*args, **kwargs):
        nonlocal sent
        sent += 1
        return original_push(*args, **kwargs)

    ntfy.push = counted_push  # type: ignore[assignment]
    try:
        return run(state), sent
    finally:
        ntfy.push = original_push  # type: ignore[assignment]


def _send_topic_dispatch_feedback(topic: str) -> None:
    ntfy.push(
        title="Run complete",
        message=f"Checked {topic}. No new notifications sent.",
        tags="white_check_mark",
        priority="high",
        topic="control",
    )


def _send_topic_dispatch_failure(topic: str) -> None:
    ntfy.push(
        title="Run failed",
        message=f"Checked {topic}, but it hit an error and could not finish. "
                f"Check the GitHub Actions log for details.",
        tags="warning",
        priority="high",
        topic="control",
    )


def _prune_retired_health(topic_health: dict) -> list[str]:
    """Drop ``topic_health`` entries for topics that no longer exist.

    Retiring or merging a topic leaves its last health stamp behind forever
    (the August 2026 audit found three: ``beach_day``, and ``health_tip`` /
    ``wikiquote`` from the spark consolidation). They are not harmless — the
    dashboard counts them as tracked topics, the weekly recap reads them, and
    every "N topics tracked" line the watchdog logs is inflated by topics that
    cannot possibly report again.

    Compares against the FULL registry, never the ``NOTIFY_ONLY`` subset, so a
    twitch-only fast-lane run can't wipe the other 39 topics' history. Returns
    the dropped names for logging.
    """
    known = {name for name, _ in TOPICS}
    retired = sorted(set(topic_health) - known)
    for name in retired:
        topic_health.pop(name, None)
    return retired


def _watchdog_failed(topics: list[tuple[str, Topic]], topic_health: dict,
                     run_ts: str) -> bool:
    """True when the watchdog ran on THIS invocation and failed on it.

    Keyed on ``last_error_ts == run_ts`` rather than on the mere presence of an
    error, so a stale failure left over from a previous run cannot keep every
    subsequent run red. Returns False when the watchdog wasn't selected at all
    (a ``NOTIFY_ONLY`` fast-lane or /run dispatch), since it can hardly be
    called broken on a run it never took part in.
    """
    if not any(name == watchdog.TOPIC for name, _ in topics):
        return False
    entry = topic_health.get(watchdog.TOPIC)
    if not isinstance(entry, dict):
        return False
    return bool(entry.get("last_error")) and entry.get("last_error_ts") == run_ts


def _warn_unreadable_config(log: logging.Logger) -> None:
    """Push a heads-up when monitors.json could not be read.

    config.load() deliberately fails soft (an empty dict, so a typo can't crash
    a scheduled run), but empty config means every topic sees "nothing
    configured" and no-ops. Runs stay green, no topic records an error, and the
    watchdog has nothing to find — the watcher just goes quiet forever. Since
    monitors.json is edited straight on github.com, one stray comma can do that,
    so the degraded read gets announced instead of only logged.

    Best-effort by design: if this push itself fails, log it and continue into
    the sweep rather than aborting the run over a notification.
    """
    reason = config.last_error()
    if not reason:
        return
    log.error("configuration unreadable: %s", reason)
    try:
        ntfy.push(
            title="monitors.json could not be read",
            message=f"{reason}\n\nEvery topic is running with EMPTY configuration, "
                    "so most checks will silently do nothing until this is fixed. "
                    "Runs will still look green.",
            tags="warning",
            priority="high",
            topic="system",
        )
    except Exception as exc:  # noqa: BLE001 - never abort the sweep over an alert
        log.error("could not push the config-failure alert: %s", exc)


def _send_topic_dispatch_unknown(names: list[str]) -> None:
    joined = ", ".join(names)
    ntfy.push(
        title="Unknown topic",
        message=f"No topic named {joined}. Nothing was checked. "
                f"Double-check the spelling.",
        tags="grey_question",
        priority="high",
        topic="control",
    )


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
            topic="system",
        )
        log.info("test push sent")
        return 0

    state = state_mod.load()

    # Before anything reads config: an unreadable monitors.json disables every
    # topic without failing anything, which is the quietest failure this system
    # has. Announce it, then carry on with the degraded run.
    config.load()
    _warn_unreadable_config(log)

    topic_dispatch = _is_topic_dispatch_run()

    # Reply-button/control channel: poll + dispatch BEFORE the topic loop so a
    # mutating command takes effect in the same run that reads it. Free-text
    # diagnostics ("status games") reply immediately from the current state.
    # The transport is Discord (a private DISCORD_CONTROL_CHANNEL); the legacy
    # ntfy poll is kept but is a no-op unless NTFY_CONTROL_TOPIC is still set.
    # Both feed the same control.dispatch — the command grammar is shared.
    # Scheduled runs, including the frequent twitch-only run, keep this control
    # latency. /run-style workflow_dispatch+NOTIFY_ONLY skips it so an on-demand
    # topic check cannot flush unrelated LATER/MORE or status/explain replies —
    # unless NOTIFY_PROCESS_PENDING explicitly opts this run back in.
    if topic_dispatch and not _force_pending_flush():
        log.info("single-topic dispatch active: skipping control channel/pending work")
    else:
        if topic_dispatch:
            log.info("single-topic dispatch: NOTIFY_PROCESS_PENDING set, "
                     "processing control/pending work anyway")
        try:
            control.dispatch(control.poll(state), state)            # legacy ntfy (off by default)
            control.dispatch(discord_control.poll(state), state)    # Discord control channel
            # Due "remind later" re-fires and queued "show more" pushes ride the
            # same cadence as the poll (every run, incl. the 15-min twitch runs).
            control.process_pending(state)
        except Exception as exc:  # noqa: BLE001 - control errors are never fatal
            log.error("control channel failed: %s", exc)

    # Enable the daily-only topics on the first run past DAILY_UTC_HOUR. Setting
    # the env var keeps each daily topic's existing NOTIFY_DAILY check unchanged.
    if _is_daily_run():
        os.environ["NOTIFY_DAILY"] = "1"
        log.info("daily run active: digest flush and learn enabled")

    # Per-topic run telemetry for the dashboard. priority.decide/emit never see a
    # topic that THREW (it never emits), so the only place that knows a topic failed
    # is this loop. Stamp last-ok / last-error here; the dashboard turns it into the
    # "topic health / last successful run / failures" panels (docs/design/02-dashboard).
    topic_health: dict = state.setdefault("topic_health", {})
    retired = _prune_retired_health(topic_health)
    if retired:
        log.info("dropped health entries for retired topic(s): %s",
                 ", ".join(retired))
    run_ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
    ok_count = fail_count = 0

    topics = _selected_topics(os.environ.get("NOTIFY_ONLY", ""))
    feedback_topic = topics[0][0] if topic_dispatch and len(topics) == 1 else ""
    # _selected_topics silently drops names that aren't real topics, so a
    # misspelled /run topic:x would otherwise check nothing and reply nothing.
    # Call it out on a dispatch run — independently of the success/failure reply,
    # since a mixed list (games,bogus) can both run a topic and name an unknown.
    known_names = {name for name, _ in TOPICS}
    requested = [n.strip() for n in os.environ.get("NOTIFY_ONLY", "").split(",") if n.strip()]
    unknown = [n for n in requested if n not in known_names]
    if topic_dispatch and unknown:
        _send_topic_dispatch_unknown(unknown)
    if len(topics) != len(TOPICS):
        log.info("NOTIFY_ONLY active: running %d of %d topics", len(topics), len(TOPICS))

    for name, run in topics:
        log.info("[%s] starting", name)
        entry = topic_health.setdefault(name, {})
        sent_count = 0
        try:
            if name == feedback_topic:
                state, sent_count = _run_counting_pushes(run, state)
            else:
                state = run(state)
        except Exception as exc:  # noqa: BLE001 - we deliberately swallow
            health.consume(state, name)  # discard any half-written report
            entry["last_error"] = str(exc)
            entry["last_error_ts"] = run_ts
            entry.pop("source_failed", None)
            fail_count += 1
            if name == feedback_topic:
                _send_topic_dispatch_failure(name)
            log.error("[%s] failed: %s", name, exc)
            log.debug("[%s] traceback:\n%s", name, traceback.format_exc())
            continue
        status = health.consume(state, name)
        if _record_outcome(entry, status,
                           adopted=name in health.ADOPTED, run_ts=run_ts):
            ok_count += 1
            log.info("[%s] ok", name)
            if name == feedback_topic and sent_count == 0:
                _send_topic_dispatch_feedback(name)
        else:
            fail_count += 1
            if name == feedback_topic:
                _send_topic_dispatch_failure(name)
            log.error("[%s] source failed: %s", name, entry["last_error"])

    # The contract reports are per-run scratch; never persist them.
    state.pop(health.STATUS_KEY, None)
    state["last_run"] = {"ts": run_ts, "ok": ok_count, "failed": fail_count}
    state_mod.save(state)

    # Exit 0 for ordinary per-topic failures: a transient network error is
    # already logged above, the watchdog will report it, and it must not turn
    # the scheduled workflow red.
    #
    # The ONE exception is the watchdog itself. Every other topic's failure has
    # a reporter; the watchdog's does not — it skips its own health entry
    # precisely because it cannot alert about being broken while it is broken.
    # A watchdog that dies (bad state shape, Discord refusing its push) would
    # otherwise take the whole monitoring layer down silently, which is the
    # exact failure class this audit was about. Escalating to a non-zero exit
    # hands it to the layer above: the run goes red and alert.yml, which runs
    # outside this process and has its own Discord path, reports it.
    if _watchdog_failed(topics, topic_health, run_ts):
        log.error("watchdog itself failed; exiting non-zero so alert.yml reports it")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
