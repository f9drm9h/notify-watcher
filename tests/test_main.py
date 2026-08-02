"""Tests for the topic-selection filter and health stamping in notify_watcher.main.

`NOTIFY_ONLY` lets the lightweight workflow mode (the 15-minute Twitch check) run a single
topic without invoking the full sweep. These pin the pure filter: blank -> all,
allowlist -> subset in declared order, unknown names ignored.

`_record_outcome` is the topic-health stamping rule: legacy topics keep
"didn't raise == last_ok", topics on the health contract (health.ADOPTED) get
last_ok only for a true ok report, soft source failures land in last_error
without touching last_ok, and a no-claim run leaves the entry alone so a soft
failure stays sticky until a true success.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from notify_watcher import health, main

RUN_TS = "2026-06-12T12:00:00+00:00"
OLD_TS = "2026-06-09T12:00:00+00:00"


class SelectedTopicsTest(unittest.TestCase):
    def test_blank_returns_all_topics(self):
        self.assertEqual(main._selected_topics(""), main.TOPICS)
        self.assertEqual(main._selected_topics("   "), main.TOPICS)

    def test_single_topic_allowlist(self):
        sel = main._selected_topics("twitch")
        self.assertEqual([n for n, _ in sel], ["twitch"])

    def test_multiple_preserve_declared_order(self):
        # Order follows TOPICS, not the order given in the env var.
        sel = main._selected_topics("iss,twitch")
        names = [n for n, _ in sel]
        self.assertEqual(set(names), {"twitch", "iss"})
        declared = [n for n, _ in main.TOPICS]
        self.assertEqual(names, [n for n in declared if n in {"twitch", "iss"}])

    def test_unknown_names_ignored(self):
        self.assertEqual(main._selected_topics("nope,twitch,alsonope"),
                         [(n, r) for n, r in main.TOPICS if n == "twitch"])

    def test_runnable_topic_is_callable(self):
        sel = main._selected_topics("twitch")
        self.assertTrue(callable(sel[0][1]))


class RecordOutcomeTest(unittest.TestCase):
    def test_legacy_topic_without_report_stamps_last_ok(self):
        entry = {"last_error": "old boom", "last_error_ts": OLD_TS}
        ok = main._record_outcome(entry, None, adopted=False, run_ts=RUN_TS)
        self.assertTrue(ok)
        self.assertEqual(entry, {"last_ok": RUN_TS})

    def test_adopted_topic_without_report_leaves_entry_untouched(self):
        # The sticky-soft-failure rule: fuel's gated 3-hourly run must not
        # wipe the soft failure its daily run recorded.
        entry = {"last_ok": OLD_TS, "last_error": "listing fetch failed",
                 "last_error_ts": OLD_TS, "source_failed": True}
        before = dict(entry)
        ok = main._record_outcome(entry, None, adopted=True, run_ts=RUN_TS)
        self.assertTrue(ok)  # the run itself is fine; it just made no claim
        self.assertEqual(entry, before)

    def test_ok_report_stamps_last_ok_and_clears_errors(self):
        entry = {"last_ok": OLD_TS, "last_error": "boom",
                 "last_error_ts": OLD_TS, "source_failed": True}
        status = {"ok": True, "source_failed": False, "data_count": 6,
                  "message": ""}
        ok = main._record_outcome(entry, status, adopted=True, run_ts=RUN_TS)
        self.assertTrue(ok)
        self.assertEqual(entry, {"last_ok": RUN_TS, "last_data_count": 6})

    def test_empty_ok_report_starts_the_quiet_clock(self):
        # ok + zero items: the source answered, so this is NOT an outage and
        # last_ok is correct — but it starts the clock watchdog.empty_days ages.
        entry: dict = {}
        status = {"ok": True, "source_failed": False, "data_count": 0,
                  "message": ""}
        self.assertTrue(main._record_outcome(entry, status, adopted=True,
                                             run_ts=RUN_TS))
        self.assertEqual(entry["last_ok"], RUN_TS)
        self.assertEqual(entry["empty_since"], RUN_TS)
        self.assertEqual(entry["empty_runs"], 1)

    def test_quiet_clock_anchors_on_the_first_empty_run(self):
        # empty_since must NOT advance while the topic stays quiet, or the
        # outage could never age past the threshold.
        entry = {"empty_since": OLD_TS, "empty_runs": 4}
        status = {"ok": True, "source_failed": False, "data_count": 0,
                  "message": ""}
        main._record_outcome(entry, status, adopted=True, run_ts=RUN_TS)
        self.assertEqual(entry["empty_since"], OLD_TS)
        self.assertEqual(entry["empty_runs"], 5)

    def test_items_returning_clears_the_quiet_clock(self):
        entry = {"empty_since": OLD_TS, "empty_runs": 9}
        status = {"ok": True, "source_failed": False, "data_count": 3,
                  "message": ""}
        main._record_outcome(entry, status, adopted=True, run_ts=RUN_TS)
        self.assertNotIn("empty_since", entry)
        self.assertNotIn("empty_runs", entry)
        self.assertEqual(entry["last_data_count"], 3)

    def test_source_failed_report_records_soft_failure_without_last_ok(self):
        entry = {"last_ok": OLD_TS}
        status = {"ok": False, "source_failed": True, "data_count": 0,
                  "message": "listing fetch failed: HTTP 403"}
        ok = main._record_outcome(entry, status, adopted=True, run_ts=RUN_TS)
        self.assertFalse(ok)
        self.assertEqual(entry["last_ok"], OLD_TS)  # NOT refreshed
        self.assertEqual(entry["last_error"], "listing fetch failed: HTTP 403")
        self.assertEqual(entry["last_error_ts"], RUN_TS)
        self.assertTrue(entry["source_failed"])

    def test_source_failed_report_with_blank_message_still_records(self):
        entry: dict = {}
        status = {"ok": False, "source_failed": True, "data_count": 0,
                  "message": ""}
        self.assertFalse(main._record_outcome(entry, status, adopted=True,
                                              run_ts=RUN_TS))
        self.assertEqual(entry["last_error"], "source failed")


class MainLoopHealthTest(unittest.TestCase):
    """End-to-end through main.main() with stub topics and state I/O mocked."""

    def _run_main(self, topics, state):
        from unittest import mock
        import os
        with mock.patch.object(main, "TOPICS", topics), \
                mock.patch.object(main.state_mod, "load", return_value=state), \
                mock.patch.object(main.state_mod, "save") as save, \
                mock.patch.object(main, "_is_daily_run", return_value=False), \
                mock.patch.dict(os.environ, {"NOTIFY_ONLY": "",
                                             "NOTIFY_TEST_PUSH": "",
                                             "NTFY_CONTROL_TOPIC": ""}):
            self.assertEqual(main.main(), 0)
        save.assert_called_once()
        return save.call_args[0][0]

    def test_soft_failure_recorded_and_scratch_never_persisted(self):
        def fake_fuel(state):
            health.source_failed(state, "fuel", "listing fetch failed: boom")
            return state

        saved = self._run_main([("fuel", fake_fuel)], {})
        entry = saved["topic_health"]["fuel"]
        self.assertNotIn("last_ok", entry)
        self.assertEqual(entry["last_error"], "listing fetch failed: boom")
        self.assertTrue(entry["source_failed"])
        self.assertNotIn(health.STATUS_KEY, saved)
        self.assertEqual(saved["last_run"]["failed"], 1)

    def test_raise_after_report_discards_the_report(self):
        def exploding(state):
            health.source_ok(state, "fuel", data_count=3)
            raise RuntimeError("post-report crash")

        saved = self._run_main([("fuel", exploding)], {})
        entry = saved["topic_health"]["fuel"]
        self.assertEqual(entry["last_error"], "post-report crash")
        self.assertNotIn("last_ok", entry)
        self.assertNotIn(health.STATUS_KEY, saved)

    def test_ok_report_counts_ok_and_stamps(self):
        def healthy(state):
            health.source_ok(state, "fuel", data_count=2)
            return state

        saved = self._run_main([("fuel", healthy)], {})
        entry = saved["topic_health"]["fuel"]
        self.assertIn("last_ok", entry)
        self.assertIn("last_data", entry)
        self.assertEqual(saved["last_run"]["ok"], 1)


class WatchdogEscalationTest(unittest.TestCase):
    """main() exits non-zero when the WATCHDOG itself failed, and only then.

    Every other topic has something that reports its failure — the watchdog. The
    watchdog has nothing, because it skips its own health entry precisely so it
    cannot try to alert about being broken while it is broken. Exiting non-zero
    hands that one case to the layer above, where alert.yml (a separate process
    with its own Discord path) reports it. Per-topic failures must still exit 0,
    or every transient network blip would turn the workflow red.
    """

    def _run_main(self, topics, expected_code):
        with mock.patch.object(main, "TOPICS", topics), \
                mock.patch.object(main.state_mod, "load", return_value={}), \
                mock.patch.object(main.state_mod, "save"), \
                mock.patch.object(main, "_is_daily_run", return_value=False), \
                mock.patch.dict(os.environ, {"NOTIFY_ONLY": "",
                                             "NOTIFY_TEST_PUSH": "",
                                             "NTFY_CONTROL_TOPIC": ""}):
            self.assertEqual(main.main(), expected_code)

    def test_a_failing_watchdog_exits_non_zero(self):
        def broken(state):
            raise RuntimeError("watchdog state is malformed")

        self._run_main([("watchdog", broken)], 1)

    def test_an_ordinary_topic_failure_still_exits_zero(self):
        def broken(state):
            raise RuntimeError("connection refused")

        self._run_main([("fuel", broken), ("watchdog", lambda s: s)], 0)

    def test_a_healthy_watchdog_exits_zero(self):
        self._run_main([("watchdog", lambda s: s)], 0)

    def test_a_stale_watchdog_error_does_not_keep_the_run_red(self):
        # Keyed on last_error_ts == this run's stamp, so an error left over from
        # a previous run cannot pin every future run to a non-zero exit.
        state = {"topic_health": {"watchdog": {"last_error": "old boom",
                                               "last_error_ts": OLD_TS}}}
        with mock.patch.object(main, "TOPICS", [("watchdog", lambda s: s)]), \
                mock.patch.object(main.state_mod, "load", return_value=state), \
                mock.patch.object(main.state_mod, "save"), \
                mock.patch.object(main, "_is_daily_run", return_value=False), \
                mock.patch.dict(os.environ, {"NOTIFY_ONLY": "",
                                             "NOTIFY_TEST_PUSH": "",
                                             "NTFY_CONTROL_TOPIC": ""}):
            self.assertEqual(main.main(), 0)

    def test_a_run_that_never_selected_the_watchdog_exits_zero(self):
        # The 15-minute fast lane and /run dispatches skip it; it can hardly be
        # called broken on a run it never took part in.
        health_map = {"watchdog": {"last_error": "x", "last_error_ts": RUN_TS}}
        self.assertFalse(main._watchdog_failed([("twitch", lambda s: s)],
                                               health_map, RUN_TS))


class PruneRetiredHealthTest(unittest.TestCase):
    """Retired topics must not linger in topic_health forever.

    The August 2026 audit found three (beach_day, plus health_tip and wikiquote
    from the spark consolidation) still being counted as tracked topics months
    after the code that could report on them was deleted.
    """

    def test_entries_for_unregistered_topics_are_dropped(self):
        th = {"fx": {"last_ok": RUN_TS}, "wikiquote": {"last_ok": OLD_TS},
              "health_tip": {"last_ok": OLD_TS}, "beach_day": {"last_ok": OLD_TS}}
        dropped = main._prune_retired_health(th)
        self.assertEqual(dropped, ["beach_day", "health_tip", "wikiquote"])
        self.assertEqual(list(th), ["fx"])

    def test_registered_topics_are_never_touched(self):
        th = {name: {"last_ok": RUN_TS} for name, _ in main.TOPICS}
        before = dict(th)
        self.assertEqual(main._prune_retired_health(th), [])
        self.assertEqual(th, before)

    def test_a_notify_only_run_cannot_wipe_the_other_topics(self):
        # The comparison is against the FULL registry, not the selected subset —
        # otherwise the 15-minute twitch fast lane would erase 39 topics of
        # history on every single run.
        th = {name: {"last_ok": RUN_TS} for name, _ in main.TOPICS}
        with mock.patch.dict(os.environ, {"NOTIFY_ONLY": "twitch,habits"}):
            self.assertEqual(main._prune_retired_health(th), [])
        self.assertEqual(len(th), len(main.TOPICS))

    def test_empty_health_is_a_noop(self):
        th = {}
        self.assertEqual(main._prune_retired_health(th), [])
        self.assertEqual(th, {})


class UnreadableConfigTest(unittest.TestCase):
    """An unparseable monitors.json is the quietest failure in the system.

    config.load() fails soft to an empty dict so a typo can't crash a scheduled
    run — but empty config means every topic sees "nothing configured" and
    no-ops. Runs stay green, no topic records an error, and the watchdog has
    nothing to find. Since the file is edited straight on github.com, one stray
    comma can silence everything indefinitely.
    """

    def test_a_broken_config_pushes_an_alert(self):
        with mock.patch.object(main.config, "last_error",
                               return_value="monitors.json is not valid JSON: line 9"), \
                mock.patch.object(main.ntfy, "push") as push:
            main._warn_unreadable_config(main.logging.getLogger("test"))
        push.assert_called_once()
        kwargs = push.call_args.kwargs
        self.assertIn("monitors.json", kwargs["title"])
        self.assertIn("line 9", kwargs["message"])
        self.assertEqual(kwargs["priority"], "high")

    def test_a_readable_config_says_nothing(self):
        with mock.patch.object(main.config, "last_error", return_value=None), \
                mock.patch.object(main.ntfy, "push") as push:
            main._warn_unreadable_config(main.logging.getLogger("test"))
        push.assert_not_called()

    def test_a_failed_config_alert_does_not_abort_the_sweep(self):
        # Best-effort by design: losing the notification must not cost the run.
        with mock.patch.object(main.config, "last_error", return_value="boom"), \
                mock.patch.object(main.ntfy, "push",
                                  side_effect=RuntimeError("discord down")):
            main._warn_unreadable_config(main.logging.getLogger("test"))


class OnDemandSingleTopicTest(unittest.TestCase):
    """The /run <topic> decoupling, proven at the main.main() level.

    A /run games dispatch sets NOTIFY_ONLY=games but leaves NOTIFY_DAILY unset
    (the workflow no longer couples the two). The behavior that must hold:
    a single-topic on-demand run of a *weekly* topic must NOT stamp its week
    guard, so it can never silently consume (and thus skip) the real scheduled
    weekly run. The topic-level early-return is pinned in test_games; this pins
    the same guarantee through the full selection + daily-gate path.
    """

    def test_on_demand_games_run_does_not_stamp_week_guard(self):
        import os
        from unittest import mock

        from notify_watcher.topics import games
        from tests._util import capture_pushes

        # _is_daily_run() is mocked False to model "daily mode is off"
        # deterministically — without it the UTC-hour fallback would set
        # NOTIFY_DAILY on any run past 12:00 UTC and make this time-dependent.
        with mock.patch.object(main, "TOPICS", [("games", games.run)]), \
                mock.patch.object(main.state_mod, "load", return_value={}), \
                mock.patch.object(main.state_mod, "save") as save, \
                mock.patch.object(main, "_is_daily_run", return_value=False), \
                mock.patch.dict(os.environ, {"NOTIFY_ONLY": "games",
                                             "NOTIFY_DAILY": "",
                                             "NOTIFY_TEST_PUSH": "",
                                             "NTFY_CONTROL_TOPIC": ""}), \
                capture_pushes() as sent:
            self.assertEqual(main.main(), 0)

        saved = save.call_args[0][0]
        # Core guarantee: the weekly slot is left intact for the scheduled run.
        self.assertNotIn(games.WEEK_STATE_KEY, saved)
        # Non-vacuous: games was actually selected and ran (legacy no-report ->
        # counts ok) but, with daily off, did nothing and emitted nothing.
        self.assertEqual(saved["last_run"]["ok"], 1)
        self.assertEqual(saved["last_run"]["failed"], 0)
        self.assertEqual(sent, [])


class MainLoopControlPhaseTest(unittest.TestCase):
    """Control/pending work runs on scheduled sweeps, not /run topic dispatches."""

    def _run_main(self, *, event_name: str, notify_only: str,
                  process_pending: str = "", scheduled: str = ""):
        calls: list[str] = []

        def selected_topic(state):
            calls.append("topic")
            return state

        state: dict = {}
        with mock.patch.object(main, "TOPICS", [(notify_only or "movies", selected_topic)]), \
                mock.patch.object(main.state_mod, "load", return_value=state), \
                mock.patch.object(main.state_mod, "save") as save, \
                mock.patch.object(main, "_is_daily_run", return_value=False), \
                mock.patch.object(main.control, "poll", return_value=["status fx"]) as ntfy_poll, \
                mock.patch.object(main.discord_control, "poll",
                                  return_value=["explain movies"]) as discord_poll, \
                mock.patch.object(main.control, "dispatch",
                                  side_effect=lambda commands, state: calls.append(
                                      f"dispatch:{commands[0]}")), \
                mock.patch.object(main.control, "process_pending",
                                  side_effect=lambda state: calls.append("pending")) as pending, \
                mock.patch.object(main.ntfy, "push") as push, \
                mock.patch.dict(os.environ, {
                    "GITHUB_EVENT_NAME": event_name,
                    "NOTIFY_ONLY": notify_only,
                    "NOTIFY_PROCESS_PENDING": process_pending,
                    "NOTIFY_SCHEDULED": scheduled,
                    "NOTIFY_TEST_PUSH": "",
                    "NTFY_CONTROL_TOPIC": "",
                }, clear=False):
            self.assertEqual(main.main(), 0)

        save.assert_called_once()
        return calls, ntfy_poll, discord_poll, pending, push

    def test_topic_workflow_dispatch_skips_control_and_pending_work(self):
        calls, ntfy_poll, discord_poll, pending, _ = self._run_main(
            event_name="workflow_dispatch", notify_only="movies")

        self.assertEqual(calls, ["topic"])
        ntfy_poll.assert_not_called()
        discord_poll.assert_not_called()
        pending.assert_not_called()

    def test_scheduled_notify_only_keeps_control_and_pending_work(self):
        calls, ntfy_poll, discord_poll, pending, _ = self._run_main(
            event_name="schedule", notify_only="twitch")

        self.assertEqual(calls, ["dispatch:status fx",
                                 "dispatch:explain movies",
                                 "pending",
                                 "topic"])
        ntfy_poll.assert_called_once()
        discord_poll.assert_called_once()
        pending.assert_called_once()

    def test_worker_cadence_dispatch_keeps_control_and_pending_work(self):
        # With GitHub's schedule trigger gone, cadence runs arrive as
        # workflow_dispatch from the Cloudflare Worker cron, marked with
        # NOTIFY_SCHEDULED=1. The twitch fast lane must keep draining control
        # + pending work every 15 minutes — treating it as a /run topic
        # dispatch would delay reply buttons to the 3-hourly full sweep.
        calls, ntfy_poll, discord_poll, pending, _ = self._run_main(
            event_name="workflow_dispatch", notify_only="twitch", scheduled="1")

        self.assertEqual(calls, ["dispatch:status fx",
                                 "dispatch:explain movies",
                                 "pending",
                                 "topic"])
        ntfy_poll.assert_called_once()
        discord_poll.assert_called_once()
        pending.assert_called_once()

    def test_full_manual_dispatch_keeps_control_and_pending_work(self):
        # workflow_dispatch with no NOTIFY_ONLY is a FULL manual run, not a
        # /run topic:x dispatch, so it must process control + pending like a
        # scheduled sweep. This exercises the bool(NOTIFY_ONLY) half of the
        # gate: workflow_dispatch alone must NOT skip control work.
        calls, ntfy_poll, discord_poll, pending, _ = self._run_main(
            event_name="workflow_dispatch", notify_only="")

        self.assertEqual(calls, ["dispatch:status fx",
                                 "dispatch:explain movies",
                                 "pending",
                                 "topic"])
        ntfy_poll.assert_called_once()
        discord_poll.assert_called_once()
        pending.assert_called_once()

    def test_topic_dispatch_with_process_pending_optin_runs_control(self):
        # NOTIFY_PROCESS_PENDING is the explicit opt-in: an on-demand single-
        # topic dispatch that would normally skip control work flushes due
        # LATER/MORE (and polls control) anyway, then still runs the topic.
        calls, ntfy_poll, discord_poll, pending, _ = self._run_main(
            event_name="workflow_dispatch", notify_only="movies",
            process_pending="1")

        self.assertEqual(calls, ["dispatch:status fx",
                                 "dispatch:explain movies",
                                 "pending",
                                 "topic"])
        ntfy_poll.assert_called_once()
        discord_poll.assert_called_once()
        pending.assert_called_once()

    def test_process_pending_optin_is_noop_when_not_a_topic_dispatch(self):
        # The opt-in only matters on a topic dispatch; on a scheduled run that
        # already processes pending, setting it changes nothing (no double run).
        calls, ntfy_poll, discord_poll, pending, _ = self._run_main(
            event_name="schedule", notify_only="twitch", process_pending="1")

        self.assertEqual(calls, ["dispatch:status fx",
                                 "dispatch:explain movies",
                                 "pending",
                                 "topic"])
        pending.assert_called_once()


class TopicDispatchFeedbackTest(unittest.TestCase):
    """Single-topic /run dispatches report completion when nothing was pushed."""

    def _run_main(self, *, event_name: str, notify_only: str, topic_pushes: bool = False,
                  raises: bool = False, ran: list | None = None, scheduled: str = ""):
        def selected_topic(state):
            if ran is not None:
                ran.append("movies")
            if raises:
                raise RuntimeError("boom")
            if topic_pushes:
                main.ntfy.push(title="Movie: Example", message="new", topic="movies")
            return state

        state: dict = {}
        with mock.patch.object(main, "TOPICS", [("movies", selected_topic)]), \
                mock.patch.object(main.state_mod, "load", return_value=state), \
                mock.patch.object(main.state_mod, "save"), \
                mock.patch.object(main, "_is_daily_run", return_value=False), \
                mock.patch.object(main.ntfy, "push") as push, \
                mock.patch.dict(os.environ, {
                    "GITHUB_EVENT_NAME": event_name,
                    "NOTIFY_ONLY": notify_only,
                    "NOTIFY_SCHEDULED": scheduled,
                    "NOTIFY_TEST_PUSH": "",
                    "NTFY_CONTROL_TOPIC": "",
                }, clear=False):
            self.assertEqual(main.main(), 0)
        return push

    def test_single_topic_dispatch_sends_no_new_items_feedback(self):
        push = self._run_main(event_name="workflow_dispatch", notify_only="movies")

        push.assert_called_once_with(
            title="Run complete",
            message="Checked movies. No new notifications sent.",
            tags="white_check_mark",
            priority="high",
            topic="control",
        )

    def test_single_topic_dispatch_with_real_push_sends_no_feedback(self):
        push = self._run_main(event_name="workflow_dispatch", notify_only="movies",
                              topic_pushes=True)

        push.assert_called_once_with(title="Movie: Example", message="new", topic="movies")

    def test_scheduled_notify_only_sends_no_feedback(self):
        push = self._run_main(event_name="schedule", notify_only="movies")

        push.assert_not_called()

    def test_worker_cadence_dispatch_sends_no_feedback(self):
        # A Worker-cron cadence run (workflow_dispatch + NOTIFY_SCHEDULED=1 +
        # NOTIFY_ONLY) is not a /run: it must stay silent, or the control
        # channel gets a "Run complete" push every 15 minutes.
        push = self._run_main(event_name="workflow_dispatch", notify_only="movies",
                              scheduled="1")

        push.assert_not_called()

    def test_full_manual_dispatch_sends_no_feedback(self):
        push = self._run_main(event_name="workflow_dispatch", notify_only="")

        push.assert_not_called()

    def test_unknown_topic_dispatch_sends_unknown_reply_and_runs_nothing(self):
        # /run topic:nope -> NOTIFY_ONLY=nope selects no real topic, so the loop
        # never runs; the dispatch must still say so instead of going silent.
        ran: list = []
        push = self._run_main(event_name="workflow_dispatch", notify_only="nope",
                              ran=ran)

        self.assertEqual(ran, [])
        push.assert_called_once_with(
            title="Unknown topic",
            message="No topic named nope. Nothing was checked. "
                    "Double-check the spelling.",
            tags="grey_question",
            priority="high",
            topic="control",
        )

    def test_single_topic_dispatch_failure_sends_failure_reply(self):
        # The dispatched topic raised: report the failure rather than nothing.
        push = self._run_main(event_name="workflow_dispatch", notify_only="movies",
                              raises=True)

        push.assert_called_once_with(
            title="Run failed",
            message="Checked movies, but it hit an error and could not finish. "
                    "Check the GitHub Actions log for details.",
            tags="warning",
            priority="high",
            topic="control",
        )

    def test_scheduled_unknown_topic_sends_no_feedback(self):
        # The unknown reply is gated on a real /run (workflow_dispatch); a
        # scheduled NOTIFY_ONLY run with an unknown name must stay silent.
        push = self._run_main(event_name="schedule", notify_only="nope")

        push.assert_not_called()


if __name__ == "__main__":
    unittest.main()
