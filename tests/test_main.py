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

    def _run_main(self, *, event_name: str, notify_only: str):
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
                mock.patch.dict(os.environ, {
                    "GITHUB_EVENT_NAME": event_name,
                    "NOTIFY_ONLY": notify_only,
                    "NOTIFY_TEST_PUSH": "",
                    "NTFY_CONTROL_TOPIC": "",
                }, clear=False):
            self.assertEqual(main.main(), 0)

        save.assert_called_once()
        return calls, ntfy_poll, discord_poll, pending

    def test_topic_workflow_dispatch_skips_control_and_pending_work(self):
        calls, ntfy_poll, discord_poll, pending = self._run_main(
            event_name="workflow_dispatch", notify_only="movies")

        self.assertEqual(calls, ["topic"])
        ntfy_poll.assert_not_called()
        discord_poll.assert_not_called()
        pending.assert_not_called()

    def test_scheduled_notify_only_keeps_control_and_pending_work(self):
        calls, ntfy_poll, discord_poll, pending = self._run_main(
            event_name="schedule", notify_only="twitch")

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
        calls, ntfy_poll, discord_poll, pending = self._run_main(
            event_name="workflow_dispatch", notify_only="")

        self.assertEqual(calls, ["dispatch:status fx",
                                 "dispatch:explain movies",
                                 "pending",
                                 "topic"])
        ntfy_poll.assert_called_once()
        discord_poll.assert_called_once()
        pending.assert_called_once()


if __name__ == "__main__":
    unittest.main()
