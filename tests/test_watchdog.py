"""Tests for the watchdog self-monitoring topic (notify_watcher.topics.watchdog)."""
from __future__ import annotations

import datetime as dt
import os
import unittest
from unittest import mock

from notify_watcher import health, main, ntfy
from notify_watcher.topics import fuel, watchdog
from tests._util import capture_pushes

NOW = dt.datetime(2026, 6, 9, 12, 0, tzinfo=dt.timezone.utc)


def _iso(hours_ago: float) -> str:
    return (NOW - dt.timedelta(hours=hours_ago)).isoformat()


class EvaluateTest(unittest.TestCase):
    def test_empty_health_is_silent(self):
        alerts, fs, al = watchdog._evaluate({}, {}, {}, NOW, 48)
        self.assertEqual(alerts, [])
        self.assertEqual(fs, {})
        self.assertEqual(al, {})

    def test_healthy_topics_never_alert(self):
        health = {"fx": {"last_ok": _iso(1)}}
        alerts, fs, al = watchdog._evaluate(health, {}, {}, NOW, 48)
        self.assertEqual(alerts, [])
        self.assertEqual(fs, {})

    def test_fresh_failure_under_threshold_is_silent_but_tracked(self):
        health = {"fda": {"last_ok": _iso(5), "last_error": "boom",
                          "last_error_ts": _iso(2)}}
        alerts, fs, al = watchdog._evaluate(health, {}, {}, NOW, 48)
        self.assertEqual(alerts, [])
        self.assertIn("fda", fs)  # outage observed, clock running
        self.assertNotIn("fda", al)

    def test_stale_failure_alerts_once_with_last_ok_anchor(self):
        health = {"fda": {"last_ok": _iso(72), "last_error": "HTTP 500",
                          "last_error_ts": _iso(1)}}
        alerts, fs, al = watchdog._evaluate(health, {}, {}, NOW, 48)
        self.assertEqual(len(alerts), 1)
        name, anchor, error = alerts[0]
        self.assertEqual(name, "fda")
        self.assertEqual(anchor, NOW - dt.timedelta(hours=72))
        self.assertEqual(error, "HTTP 500")
        self.assertIn("fda", al)
        # Same outage on the next run: already alerted, stays silent.
        alerts2, _, al2 = watchdog._evaluate(health, fs, al, NOW, 48)
        self.assertEqual(alerts2, [])
        self.assertIn("fda", al2)

    def test_recovery_rearms_for_the_next_outage(self):
        failing = {"fda": {"last_ok": _iso(72), "last_error": "boom",
                           "last_error_ts": _iso(1)}}
        alerts, fs, al = watchdog._evaluate(failing, {}, {}, NOW, 48)
        self.assertEqual(len(alerts), 1)
        # Topic recovers: main.py clears last_error and stamps a new last_ok.
        recovered = {"fda": {"last_ok": _iso(0)}}
        alerts, fs, al = watchdog._evaluate(recovered, fs, al, NOW, 48)
        self.assertEqual(alerts, [])
        self.assertEqual(fs, {})
        self.assertEqual(al, {})
        # A later, second outage alerts again.
        later = NOW + dt.timedelta(hours=100)
        failing2 = {"fda": {"last_ok": _iso(0), "last_error": "down again",
                            "last_error_ts": later.isoformat()}}
        alerts, fs, al = watchdog._evaluate(failing2, fs, al, later, 48)
        self.assertEqual(len(alerts), 1)

    def test_never_succeeded_topic_clocks_from_first_observation(self):
        # No last_ok at all: the first sighting seeds failing_since from
        # last_error_ts and stays silent...
        health = {"newtopic": {"last_error": "no such feed",
                               "last_error_ts": _iso(0)}}
        alerts, fs, al = watchdog._evaluate(health, {}, {}, NOW, 48)
        self.assertEqual(alerts, [])
        self.assertEqual(fs["newtopic"], _iso(0))
        # ...and alerts once that observed start is stale_hours old.
        later = NOW + dt.timedelta(hours=49)
        health["newtopic"]["last_error_ts"] = later.isoformat()
        alerts, fs, al = watchdog._evaluate(health, fs, al, later, 48)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0][0], "newtopic")

    def test_malformed_timestamps_fall_back_without_crashing(self):
        health = {"fda": {"last_ok": "not-a-date", "last_error": "boom",
                          "last_error_ts": _iso(60)}}
        alerts, fs, al = watchdog._evaluate(health, {"fda": _iso(60)}, {}, NOW, 48)
        self.assertEqual(len(alerts), 1)  # anchored on failing_since instead

    def test_unanchorable_outage_is_skipped_not_guessed(self):
        health = {"fda": {"last_ok": "not-a-date", "last_error": "boom"}}
        alerts, fs, al = watchdog._evaluate(health, {"fda": "also-bad"}, {}, NOW, 48)
        self.assertEqual(alerts, [])

    def test_watchdog_skips_itself(self):
        health = {"watchdog": {"last_ok": _iso(100), "last_error": "boom"}}
        alerts, fs, al = watchdog._evaluate(health, {}, {}, NOW, 48)
        self.assertEqual(alerts, [])
        self.assertEqual(fs, {})


class BuildMessageTest(unittest.TestCase):
    def test_single_outage_names_the_topic_in_the_title(self):
        anchor = NOW - dt.timedelta(hours=72)
        title, body = watchdog._build_message([("fda", anchor, "HTTP 500")], 48)
        self.assertIn("'fda'", title)
        self.assertIn("48h", title)
        self.assertIn("2026-06-06 12:00 UTC", body)
        self.assertIn("HTTP 500", body)

    def test_multiple_outages_bundle_into_one_message(self):
        anchor = NOW - dt.timedelta(hours=72)
        title, body = watchdog._build_message(
            [("energy", anchor, "x"), ("fda", anchor, "y")], 48)
        self.assertIn("2 topics", title)
        self.assertEqual(len(body.splitlines()), 2)

    def test_long_errors_are_truncated(self):
        anchor = NOW - dt.timedelta(hours=72)
        _, body = watchdog._build_message([("fda", anchor, "e" * 500)], 48)
        self.assertLess(len(body), 250)
        self.assertIn("…", body)


def _days(days_ago: float) -> str:
    return (NOW - dt.timedelta(days=days_ago)).isoformat()


class EvaluateDataTest(unittest.TestCase):
    CFG = {"fda": 14}

    def test_fresh_data_is_silent(self):
        health = {"fda": {"last_ok": _iso(1), "last_data": _days(2)}}
        alerts, bl, al = watchdog._evaluate_data(health, self.CFG, {}, {}, NOW)
        self.assertEqual(alerts, [])
        self.assertEqual(al, {})

    def test_unconfigured_topic_is_never_data_checked(self):
        health = {"energy": {"last_ok": _iso(1)}}  # no last_data, ever
        alerts, bl, al = watchdog._evaluate_data(health, self.CFG, {}, {}, NOW)
        self.assertEqual(alerts, [])
        self.assertNotIn("energy", bl)

    def test_never_stamped_topic_clocks_from_first_observation(self):
        # Enabling the check on a topic with no stamp yet must not alert
        # instantly: the first sighting seeds the baseline...
        health = {"fda": {"last_ok": _iso(1)}}
        alerts, bl, al = watchdog._evaluate_data(health, self.CFG, {}, {}, NOW)
        self.assertEqual(alerts, [])
        self.assertEqual(bl["fda"], NOW.isoformat())
        # ...and alerts once that baseline is older than the window.
        later = NOW + dt.timedelta(days=15)
        alerts, bl, al = watchdog._evaluate_data(health, self.CFG, bl, al, later)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0][0], "fda")

    def test_stale_data_alerts_once_then_stays_quiet(self):
        health = {"fda": {"last_ok": _iso(1), "last_data": _days(20)}}
        alerts, bl, al = watchdog._evaluate_data(health, self.CFG, {}, {}, NOW)
        self.assertEqual(len(alerts), 1)
        name, anchor, days = alerts[0]
        self.assertEqual((name, days), ("fda", 14.0))
        self.assertEqual(anchor, NOW - dt.timedelta(days=20))
        self.assertIn("fda", al)
        alerts2, _, al2 = watchdog._evaluate_data(health, self.CFG, bl, al, NOW)
        self.assertEqual(alerts2, [])
        self.assertIn("fda", al2)

    def test_fresh_data_rearms_for_the_next_outage(self):
        stale = {"fda": {"last_data": _days(20)}}
        alerts, bl, al = watchdog._evaluate_data(stale, self.CFG, {}, {}, NOW)
        self.assertEqual(len(alerts), 1)
        # Data flows again: alerted marker is dropped (re-armed)...
        fresh = {"fda": {"last_data": _days(0)}}
        alerts, bl, al = watchdog._evaluate_data(fresh, self.CFG, bl, al, NOW)
        self.assertEqual(alerts, [])
        self.assertEqual(al, {})
        # ...so a second outage alerts again.
        later = NOW + dt.timedelta(days=15)
        alerts, bl, al = watchdog._evaluate_data(fresh, self.CFG, bl, al, later)
        self.assertEqual(len(alerts), 1)

    def test_topic_removed_from_config_drops_its_tracking(self):
        bl = {"fda": _days(30), "old": _days(30)}
        al = {"old": _days(1)}
        _, bl2, al2 = watchdog._evaluate_data(
            {"fda": {"last_data": _days(1)}}, self.CFG, bl, al, NOW)
        self.assertNotIn("old", bl2)
        self.assertNotIn("old", al2)

    def test_invalid_day_values_are_skipped(self):
        health = {"fda": {"last_data": _days(400)}}
        for bad in ("soon", None, 0, -3):
            with self.subTest(days=bad):
                alerts, bl, al = watchdog._evaluate_data(
                    health, {"fda": bad}, {}, {}, NOW)
                self.assertEqual(alerts, [])

    def test_unparseable_baseline_restarts_the_clock(self):
        health = {"fda": {}}  # no last_data, baseline is garbage
        alerts, bl, al = watchdog._evaluate_data(
            health, self.CFG, {"fda": "not-a-date"}, {}, NOW)
        self.assertEqual(alerts, [])
        self.assertEqual(bl["fda"], NOW.isoformat())


class DataMessageTest(unittest.TestCase):
    def test_single_topic_named_in_title(self):
        anchor = NOW - dt.timedelta(days=20)
        title, body = watchdog._build_data_message([("fda", anchor, 14.0)])
        self.assertIn("'fda'", title)
        self.assertIn("14+ days", title)
        self.assertIn("2026-05-20 12:00 UTC", body)

    def test_multiple_topics_bundle(self):
        anchor = NOW - dt.timedelta(days=20)
        title, body = watchdog._build_data_message(
            [("energy", anchor, 14.0), ("fda", anchor, 14.0)])
        self.assertIn("2 topics", title)
        self.assertEqual(len(body.splitlines()), 2)


class RunTest(unittest.TestCase):
    def test_stale_outage_pushes_once_then_stays_silent(self):
        state = {"topic_health": {"fda": {
            "last_ok": _iso(72), "last_error": "HTTP 500", "last_error_ts": _iso(1),
        }}}
        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(len(sent), 1)
        self.assertIn("Watchdog", sent[0]["title"])
        self.assertIn("fda", sent[0]["title"])
        self.assertIn("fda", state[watchdog.ALERTED_KEY])
        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(sent, [])

    def test_healthy_state_sends_nothing(self):
        state = {"topic_health": {"fx": {"last_ok": _iso(1)}}}
        with capture_pushes() as sent:
            watchdog.run(state)
        self.assertEqual(sent, [])

    def test_no_health_key_is_a_noop(self):
        with capture_pushes() as sent:
            out = watchdog.run({})
        self.assertEqual(sent, [])
        self.assertEqual(out, {})

    def test_failed_alert_push_is_retried_next_run(self):
        # The push raising must leave the alerted marker unwritten so the next
        # run re-sends — a watchdog alert must not be lost to an ntfy outage.
        state = {"topic_health": {"fda": {
            "last_ok": _iso(72), "last_error": "HTTP 500", "last_error_ts": _iso(1),
        }}}
        with mock.patch.object(ntfy, "push", side_effect=RuntimeError("ntfy down")):
            with self.assertRaises(RuntimeError):
                watchdog.run(state)
        self.assertNotIn("fda", state.get(watchdog.ALERTED_KEY) or {})
        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(len(sent), 1)
        self.assertIn("fda", state[watchdog.ALERTED_KEY])

    def test_stale_data_pushes_once_then_stays_silent(self):
        # End-to-end through run() and the real monitors.json config, which
        # opts fda into data_stale_days: a healthy-but-empty fda (fresh
        # last_ok, baseline far older than any sane window) alerts once.
        state = {
            "topic_health": {"fda": {"last_ok": _iso(1)}},
            watchdog.DATA_BASELINE_KEY: {"fda": _days(60)},
        }
        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(len(sent), 1)
        self.assertIn("no data", sent[0]["title"])
        self.assertIn("fda", state[watchdog.DATA_ALERTED_KEY])
        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(sent, [])


def _iso_now(hours_ago: float) -> str:
    """ISO timestamp relative to the REAL clock, for run()-level tests."""
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(hours=hours_ago)).isoformat()


class SwallowedFuelFailureTest(unittest.TestCase):
    """End-to-end proof of the topic health contract: a fuel source failure
    that fuel.run swallows internally (log + return state) must still cross
    the watchdog's no-successful-run threshold and alert, because main.py now
    stamps last_ok only for a true ok report and a soft failure stays sticky
    across fuel's gated 3-hourly runs.
    """

    def _record(self, state: dict, name, run) -> dict:
        """Mimic main.py's per-topic loop body for one topic."""
        entry = state.setdefault("topic_health", {}).setdefault(name, {})
        run_ts = dt.datetime.now(dt.timezone.utc).isoformat()
        state = run(state)
        status = health.consume(state, name)
        main._record_outcome(entry, status, adopted=name in health.ADOPTED,
                             run_ts=run_ts)
        return state

    def _swallowed_failure_run(self, state: dict) -> dict:
        """One daily fuel run whose MICM fetch dies; fuel.run swallows it."""
        with mock.patch.object(fuel.requests, "get",
                               side_effect=OSError("connection refused")), \
                mock.patch.dict(os.environ, {"NOTIFY_DAILY": "1"}):
            return self._record(state, "fuel", fuel.run)

    def test_swallowed_fuel_failure_alerts_after_threshold(self):
        last_good = _iso_now(72)  # beyond the 48h stale_hours threshold
        state = {"topic_health": {"fuel": {"last_ok": last_good}}}

        state = self._swallowed_failure_run(state)
        entry = state["topic_health"]["fuel"]
        self.assertIn("listing fetch failed", entry["last_error"])
        self.assertTrue(entry["source_failed"])
        self.assertEqual(entry["last_ok"], last_good)  # NOT refreshed

        # A gated 3-hourly run (no NOTIFY_DAILY) makes no claim and must not
        # wipe the soft failure — the old behavior that hid dead sources.
        with mock.patch.dict(os.environ, {"NOTIFY_DAILY": ""}):
            state = self._record(state, "fuel", fuel.run)
        self.assertEqual(state["topic_health"]["fuel"]["last_error"],
                         entry["last_error"])
        self.assertEqual(state["topic_health"]["fuel"]["last_ok"], last_good)

        # The watchdog now sees 72h with no true success: one alert, then quiet.
        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(len(sent), 1)
        self.assertIn("fuel", sent[0]["title"])
        self.assertIn("listing fetch failed", sent[0]["message"])
        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(sent, [])

    def test_under_threshold_is_silent(self):
        state = {"topic_health": {"fuel": {"last_ok": _iso_now(24)}}}
        state = self._swallowed_failure_run(state)
        with capture_pushes() as sent:
            watchdog.run(state)
        self.assertEqual(sent, [])

    def test_recovery_rearms_after_alert(self):
        state = {"topic_health": {"fuel": {"last_ok": _iso_now(72)}}}
        state = self._swallowed_failure_run(state)
        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(len(sent), 1)

        def healthy_fuel(s):
            health.source_ok(s, "fuel", data_count=6)
            return s

        state = self._record(state, "fuel", healthy_fuel)
        entry = state["topic_health"]["fuel"]
        self.assertNotIn("last_error", entry)
        self.assertNotIn("source_failed", entry)
        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(sent, [])
        self.assertNotIn("fuel", state[watchdog.ALERTED_KEY])


if __name__ == "__main__":
    unittest.main()
