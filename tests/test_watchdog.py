"""Tests for the watchdog self-monitoring topic (notify_watcher.topics.watchdog).

Covers the three-phase outage lifecycle introduced by the August 2026
reliability audit — immediate first alert, periodic reminders while the outage
lasts, exactly one recovery notice — plus the anti-spam bundling and the
at-least-once delivery contract that make it safe to trust.
"""
from __future__ import annotations

import datetime as dt
import os
import unittest
from unittest import mock

from notify_watcher import health, main, ntfy
from notify_watcher.topics import fuel, watchdog
from tests._util import capture_pushes

NOW = dt.datetime(2026, 6, 9, 12, 0, tzinfo=dt.timezone.utc)
HOUR = dt.timedelta(hours=1)
DAY = dt.timedelta(days=1)


def _iso(hours_ago: float) -> str:
    return (NOW - dt.timedelta(hours=hours_ago)).isoformat()


def _days(days_ago: float) -> str:
    return (NOW - dt.timedelta(days=days_ago)).isoformat()


def _now_iso(hours_ago: float) -> str:
    """ISO timestamp relative to the REAL clock, for run()-level tests."""
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(hours=hours_ago)).isoformat()


def _failing(**topics) -> dict:
    """{name: error} -> the _track input shape, anchored at the outage start."""
    return {name: {"detail": err, "anchor": None} for name, err in topics.items()}


def _watchdog_cfg(section: dict):
    """Patch ONLY monitors.json's watchdog section, leaving the rest real.

    config.section is a shared module function: patching it wholesale would also
    hand this fake dict to events.emit's `priority` lookup, silently turning the
    priority engine off and dropping the very pushes the test is asserting on.
    """
    real = watchdog.config.section

    def fake(name):
        return section if name == "watchdog" else real(name)

    return mock.patch.object(watchdog.config, "section", side_effect=fake)


class TrackTest(unittest.TestCase):
    """The pure state machine: which transitions fire, and when."""

    DELAY0 = dt.timedelta(0)
    REMIND12 = dt.timedelta(hours=12)

    def _track(self, failing, tracked, now=NOW, delay=None, reminder=REMIND12):
        return watchdog._track(
            failing, tracked, now,
            delay=self.DELAY0 if delay is None else delay,
            reminder=reminder,
        )

    def test_nothing_failing_is_silent(self):
        new, due, rec, ledger = self._track({}, {})
        self.assertEqual((new, due, rec, ledger), ([], [], [], {}))

    def test_first_failure_alerts_immediately(self):
        # The headline change: the old design waited stale_hours (48) before
        # saying anything, so a monitor could be dead for two days in silence.
        new, due, rec, ledger = self._track(_failing(fda="HTTP 500"), {})
        self.assertEqual([t.name for t in new], ["fda"])
        self.assertEqual(due, [])
        self.assertEqual(rec, [])
        # The caller stamps the alert marker only after the push lands, so the
        # ledger the machine returns must NOT yet claim it was alerted.
        self.assertNotIn("alerted", ledger["fda"])
        self.assertEqual(ledger["fda"]["since"], NOW.isoformat())

    def test_alert_delay_holds_the_first_alert_back(self):
        # Opt-in grace period for a flaky source: nothing until the outage has
        # persisted, then the same alert.
        delay = dt.timedelta(hours=6)
        new, _, _, ledger = self._track(_failing(fda="boom"), {}, delay=delay)
        self.assertEqual(new, [])
        self.assertEqual(ledger["fda"]["since"], NOW.isoformat())

        later = NOW + 7 * HOUR
        new, _, _, _ = self._track(_failing(fda="boom"), ledger, now=later, delay=delay)
        self.assertEqual([t.name for t in new], ["fda"])

    def test_already_alerted_outage_does_not_re_alert(self):
        ledger = {"fda": {"since": _iso(3), "alerted": _iso(3),
                          "last_alert": _iso(3), "reminders": 0}}
        new, due, rec, _ = self._track(_failing(fda="boom"), ledger)
        self.assertEqual((new, due, rec), ([], [], []))

    def test_reminder_fires_once_the_interval_elapses(self):
        ledger = {"fda": {"since": _iso(20), "alerted": _iso(20),
                          "last_alert": _iso(13), "reminders": 0}}
        new, due, rec, _ = self._track(_failing(fda="still down"), ledger)
        self.assertEqual(new, [])
        self.assertEqual([t.name for t in due], ["fda"])
        self.assertEqual(due[0].reminders, 1)  # numbered so repeats stay legible
        self.assertEqual(rec, [])

    def test_reminders_number_upward_across_a_long_outage(self):
        ledger = {"fda": {"since": _iso(40), "alerted": _iso(40),
                          "last_alert": _iso(13), "reminders": 2}}
        _, due, _, _ = self._track(_failing(fda="x"), ledger)
        self.assertEqual(due[0].reminders, 3)

    def test_reminders_can_be_switched_off(self):
        ledger = {"fda": {"since": _iso(200), "alerted": _iso(200),
                          "last_alert": _iso(200), "reminders": 0}}
        _, due, _, _ = self._track(_failing(fda="x"), ledger, reminder=None)
        self.assertEqual(due, [])

    def test_recovery_fires_exactly_once_for_an_alerted_outage(self):
        ledger = {"fda": {"since": _iso(30), "alerted": _iso(30),
                          "last_alert": _iso(2), "reminders": 1}}
        new, due, rec, ledger2 = self._track({}, ledger)
        self.assertEqual((new, due), ([], []))
        self.assertEqual([t.name for t in rec], ["fda"])
        # Still tracked at this point: _deliver drops it only once the notice
        # has actually been sent, so a failed push retries instead of vanishing.
        self.assertIn("fda", ledger2)

    def test_outage_that_recovers_before_being_alerted_stays_silent(self):
        # Nothing was ever reported, so there is nothing to close out.
        ledger = {"fda": {"since": _iso(1), "reminders": 0}}
        new, due, rec, ledger2 = self._track({}, ledger)
        self.assertEqual((new, due, rec, ledger2), ([], [], [], {}))

    def test_simultaneous_outages_bundle_into_one_transition_list(self):
        # A runner-wide blip must produce ONE push, not one per topic.
        new, _, _, _ = self._track(_failing(fda="net", energy="net", quakes="net"), {})
        self.assertEqual([t.name for t in new], ["energy", "fda", "quakes"])

    def test_last_ok_is_preferred_as_the_display_anchor(self):
        failing = {"fda": {"detail": "boom", "anchor": NOW - 72 * HOUR}}
        new, _, _, _ = self._track(failing, {})
        self.assertEqual(new[0].anchor, NOW - 72 * HOUR)

    def test_topic_with_no_last_ok_anchors_on_first_sighting(self):
        new, _, _, ledger = self._track(_failing(newtopic="no such feed"), {})
        self.assertEqual(new[0].anchor, NOW)
        self.assertEqual(ledger["newtopic"]["since"], NOW.isoformat())

    def test_unparseable_ledger_entry_does_not_crash(self):
        new, due, rec, ledger = self._track(_failing(fda="boom"), {"fda": "not-a-dict"})
        self.assertEqual([t.name for t in new], ["fda"])
        self.assertEqual(ledger["fda"]["since"], NOW.isoformat())

    def test_a_second_outage_after_recovery_alerts_again(self):
        _, _, rec, ledger = self._track({}, {
            "fda": {"since": _iso(30), "alerted": _iso(30),
                    "last_alert": _iso(1), "reminders": 0}})
        self.assertEqual(len(rec), 1)
        ledger.pop("fda")  # _deliver drops it once the recovery notice lands
        later = NOW + 100 * HOUR
        new, _, _, _ = self._track(_failing(fda="down again"), ledger, now=later)
        self.assertEqual([t.name for t in new], ["fda"])


class MessageTest(unittest.TestCase):
    def test_first_alert_names_the_single_topic_and_its_error(self):
        t = watchdog.Transition("fda", NOW - 72 * HOUR, "HTTP 500")
        title, body = watchdog._build_message([t], NOW)
        self.assertIn("'fda'", title)
        self.assertIn("is failing", title)
        self.assertIn("2026-06-06 12:00 UTC", body)
        self.assertIn("3d", body)
        self.assertIn("HTTP 500", body)

    def test_first_alert_bundles_several_topics_into_one_message(self):
        anchor = NOW - 72 * HOUR
        title, body = watchdog._build_message(
            [watchdog.Transition("energy", anchor, "x"),
             watchdog.Transition("fda", anchor, "y")], NOW)
        self.assertIn("2 topics", title)
        self.assertEqual(len(body.splitlines()), 2)

    def test_reminder_says_STILL_and_carries_the_count(self):
        t = watchdog.Transition("fda", NOW - 30 * HOUR, "HTTP 500", 3)
        title, body = watchdog._build_reminder([t], NOW)
        self.assertIn("STILL", title)
        self.assertIn("1d 6h", title)
        self.assertIn("reminder #3", body)

    def test_recovery_message_reports_the_downtime(self):
        t = watchdog.Transition("fda", NOW - 26 * HOUR, "")
        title, body = watchdog._build_recovery([t], NOW)
        self.assertIn("'fda'", title)
        self.assertIn("recovered", title)
        self.assertIn("1d 2h", body)

    def test_long_errors_are_truncated(self):
        t = watchdog.Transition("fda", NOW - HOUR, "e" * 500)
        _, body = watchdog._build_message([t], NOW)
        self.assertLess(len(body), 250)
        self.assertIn("…", body)

    def test_data_messages_name_the_threshold(self):
        t = watchdog.Transition("fda", NOW - 20 * DAY, "14d")
        title, body = watchdog._build_data_message([t], NOW)
        self.assertIn("'fda'", title)
        self.assertIn("14d", title)
        self.assertIn("2026-05-20 12:00 UTC", body)
        self.assertIn("STILL", watchdog._build_data_reminder([t], NOW)[0])
        self.assertIn("producing data again",
                      watchdog._build_data_recovery([t], NOW)[0])

    def test_age_formatting_reads_naturally_at_every_scale(self):
        cases = [(dt.timedelta(minutes=40), "40m"), (dt.timedelta(hours=7), "7h"),
                 (dt.timedelta(days=3), "3d"), (dt.timedelta(days=3, hours=4), "3d 4h"),
                 (dt.timedelta(seconds=-5), "0m")]
        for delta, expected in cases:
            with self.subTest(delta=delta):
                self.assertEqual(watchdog._fmt_age(delta), expected)


class MigrationTest(unittest.TestCase):
    """The live state.json carries the pre-redesign keys; nothing may be lost."""

    def test_legacy_keys_become_an_outage_ledger(self):
        state = {
            watchdog.LEGACY_FAILING_SINCE_KEY: {"visa_bulletin": _iso(48)},
            watchdog.LEGACY_ALERTED_KEY: {"visa_bulletin": _iso(24)},
        }
        ledger = watchdog._migrate(state)
        self.assertEqual(ledger["visa_bulletin"]["since"], _iso(48))
        # Already alerted stays alerted: no duplicate alert on the deploy run,
        # and the outage still gets its recovery notice when it clears.
        self.assertEqual(ledger["visa_bulletin"]["alerted"], _iso(24))
        self.assertEqual(ledger["visa_bulletin"]["last_alert"], _iso(24))

    def test_tracked_but_unalerted_legacy_outage_is_not_marked_alerted(self):
        state = {watchdog.LEGACY_FAILING_SINCE_KEY: {"fda": _iso(3)},
                 watchdog.LEGACY_ALERTED_KEY: {}}
        self.assertNotIn("alerted", watchdog._migrate(state)["fda"])

    def test_an_existing_ledger_is_never_overwritten_by_migration(self):
        state = {watchdog.OUTAGES_KEY: {"fda": {"since": _iso(1), "reminders": 0}},
                 watchdog.LEGACY_FAILING_SINCE_KEY: {"energy": _iso(9)}}
        self.assertEqual(list(watchdog._migrate(state)), ["fda"])

    def test_run_drops_the_legacy_keys_once_migrated(self):
        state = {
            "topic_health": {"fx": {"last_ok": _iso(1)}},
            watchdog.LEGACY_FAILING_SINCE_KEY: {},
            watchdog.LEGACY_ALERTED_KEY: {},
            watchdog.LEGACY_DATA_ALERTED_KEY: {},
        }
        with capture_pushes():
            state = watchdog.run(state)
        self.assertNotIn(watchdog.LEGACY_FAILING_SINCE_KEY, state)
        self.assertNotIn(watchdog.LEGACY_ALERTED_KEY, state)
        self.assertNotIn(watchdog.LEGACY_DATA_ALERTED_KEY, state)

    def test_migrated_in_flight_outage_reminds_instead_of_re_alerting(self):
        # The visa_bulletin case found live in state.json: alerted 2026-07-16,
        # then silence for 17 days. After the redesign the next run must send a
        # REMINDER — not a duplicate first alert, and not nothing.
        state = {
            "topic_health": {"visa_bulletin": {
                "last_ok": _now_iso(400), "last_error": "index fetch failed",
                "last_error_ts": _now_iso(1)}},
            watchdog.LEGACY_FAILING_SINCE_KEY: {"visa_bulletin": _now_iso(400)},
            watchdog.LEGACY_ALERTED_KEY: {"visa_bulletin": _now_iso(360)},
        }
        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(len(sent), 1)
        self.assertIn("STILL", sent[0]["title"])


class RunOutageLifecycleTest(unittest.TestCase):
    """End-to-end through run(), against the real monitors.json config."""

    def _health(self, **kw):
        return {"topic_health": {"fda": dict(kw)}}

    def test_first_failure_pushes_immediately_then_stays_quiet(self):
        state = self._health(last_ok=_now_iso(2), last_error="HTTP 500",
                             last_error_ts=_now_iso(0.1))
        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(len(sent), 1)
        self.assertIn("is failing", sent[0]["title"])
        self.assertIn("alerted", state[watchdog.OUTAGES_KEY]["fda"])
        # Immediately after, well inside the reminder interval: silence.
        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(sent, [])

    def test_a_continuing_outage_reminds_after_the_interval(self):
        state = self._health(last_ok=_now_iso(50), last_error="HTTP 500",
                             last_error_ts=_now_iso(0.1))
        state[watchdog.OUTAGES_KEY] = {"fda": {
            "since": _now_iso(50), "alerted": _now_iso(50),
            "last_alert": _now_iso(20), "reminders": 0}}
        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(len(sent), 1)
        self.assertIn("STILL failing", sent[0]["title"])
        self.assertEqual(state[watchdog.OUTAGES_KEY]["fda"]["reminders"], 1)

    def test_recovery_pushes_exactly_one_green_notice(self):
        state = self._health(last_ok=_now_iso(50), last_error="HTTP 500",
                             last_error_ts=_now_iso(0.1))
        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(len(sent), 1)

        state["topic_health"]["fda"] = {"last_ok": _now_iso(0)}
        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(len(sent), 1)
        self.assertIn("recovered", sent[0]["title"])
        self.assertNotIn("fda", state[watchdog.OUTAGES_KEY])
        # ...and never a second one.
        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(sent, [])

    def test_many_topics_failing_at_once_send_a_single_push(self):
        state = {"topic_health": {
            name: {"last_ok": _now_iso(2), "last_error": "connection refused",
                   "last_error_ts": _now_iso(0.1)}
            for name in ("fda", "energy", "quakes", "weather", "uv")}}
        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(len(sent), 1)
        self.assertIn("5 topics are failing", sent[0]["title"])
        self.assertEqual(len(sent[0]["message"].splitlines()), 5)

    def test_outage_alerts_are_critical_so_a_mute_cannot_bury_them(self):
        # events._apply_mute exempts critical severity. Muting the topic that
        # tells you your monitors are broken must not silence it.
        state = self._health(last_ok=_now_iso(2), last_error="boom",
                             last_error_ts=_now_iso(0.1))
        with capture_pushes() as sent:
            watchdog.run(state)
        self.assertEqual(sent[0]["severity"], "critical")

    def test_healthy_state_sends_nothing(self):
        with capture_pushes() as sent:
            watchdog.run({"topic_health": {"fx": {"last_ok": _iso(1)}}})
        self.assertEqual(sent, [])

    def test_no_health_key_is_a_noop(self):
        with capture_pushes() as sent:
            out = watchdog.run({})
        self.assertEqual(sent, [])
        self.assertEqual(out, {})

    def test_watchdog_skips_its_own_health_entry(self):
        # It cannot report being broken while broken; main.py escalates that to
        # a non-zero exit instead (see test_main).
        state = {"topic_health": {"watchdog": {
            "last_ok": _iso(100), "last_error": "boom", "last_error_ts": _iso(1)}}}
        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(sent, [])
        self.assertEqual(state[watchdog.OUTAGES_KEY], {})


class DeliveryContractTest(unittest.TestCase):
    """A failed push must never be recorded as delivered."""

    def test_failed_first_alert_is_retried_next_run(self):
        state = {"topic_health": {"fda": {
            "last_ok": _now_iso(2), "last_error": "HTTP 500",
            "last_error_ts": _now_iso(0.1)}}}
        with mock.patch.object(ntfy, "push", side_effect=RuntimeError("discord down")):
            with self.assertRaises(RuntimeError):
                watchdog.run(state)
        self.assertNotIn("alerted", state[watchdog.OUTAGES_KEY].get("fda", {}))
        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(len(sent), 1)
        self.assertIn("is failing", sent[0]["title"])
        self.assertIn("alerted", state[watchdog.OUTAGES_KEY]["fda"])

    def test_failed_recovery_notice_is_retried_without_re_alerting(self):
        # This is why markers are stamped per bundle instead of once at the end:
        # losing the green push must not roll back the red one and fire it twice.
        state = {"topic_health": {"fda": {"last_ok": _now_iso(0)}},
                 watchdog.OUTAGES_KEY: {"fda": {
                     "since": _now_iso(30), "alerted": _now_iso(30),
                     "last_alert": _now_iso(1), "reminders": 0}}}
        with mock.patch.object(ntfy, "push", side_effect=RuntimeError("discord down")):
            with self.assertRaises(RuntimeError):
                watchdog.run(state)
        self.assertIn("fda", state[watchdog.OUTAGES_KEY])
        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(len(sent), 1)
        self.assertIn("recovered", sent[0]["title"])

    def test_a_lost_reminder_does_not_advance_the_reminder_count(self):
        state = {"topic_health": {"fda": {
            "last_ok": _now_iso(50), "last_error": "x",
            "last_error_ts": _now_iso(1)}},
            watchdog.OUTAGES_KEY: {"fda": {
                "since": _now_iso(50), "alerted": _now_iso(50),
                "last_alert": _now_iso(20), "reminders": 4}}}
        with mock.patch.object(ntfy, "push", side_effect=RuntimeError("discord down")):
            with self.assertRaises(RuntimeError):
                watchdog.run(state)
        self.assertEqual(state[watchdog.OUTAGES_KEY]["fda"]["reminders"], 4)


class DataStalenessTest(unittest.TestCase):
    """The opt-in 'source returns 200 but zero items' check, same lifecycle."""

    def test_stale_data_alerts_then_reminds_then_recovers(self):
        state = {
            "topic_health": {"fda": {"last_ok": _now_iso(1)}},
            watchdog.DATA_BASELINE_KEY: {"fda": _days(60)},
        }
        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(len(sent), 1)
        self.assertIn("no data", sent[0]["title"])
        self.assertIn("fda", state[watchdog.DATA_OUTAGES_KEY])

        with capture_pushes() as sent:  # inside the reminder window
            state = watchdog.run(state)
        self.assertEqual(sent, [])

        # Backdate the last alert past data_reminder_days -> one reminder.
        state[watchdog.DATA_OUTAGES_KEY]["fda"]["last_alert"] = _now_iso(24 * 30)
        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(len(sent), 1)
        self.assertIn("STILL has no data", sent[0]["title"])

        # Data flows again -> exactly one all-clear, then silence.
        state["topic_health"]["fda"]["last_data"] = _now_iso(0)
        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(len(sent), 1)
        self.assertIn("producing data again", sent[0]["title"])
        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(sent, [])

    def test_fresh_data_is_silent(self):
        state = {"topic_health": {"fda": {"last_ok": _now_iso(1),
                                          "last_data": _now_iso(24)}}}
        with capture_pushes() as sent:
            watchdog.run(state)
        self.assertEqual(sent, [])

    def test_never_stamped_topic_clocks_from_first_observation(self):
        # Enabling the check must not alert instantly on a topic with no stamp.
        state = {"topic_health": {"fda": {"last_ok": _now_iso(1)}}}
        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(sent, [])
        self.assertIn("fda", state[watchdog.DATA_BASELINE_KEY])

    def test_unconfigured_topic_is_never_data_checked(self):
        state = {"topic_health": {"astronomy": {"last_ok": _now_iso(1)}}}
        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(sent, [])
        self.assertNotIn("astronomy", state[watchdog.DATA_BASELINE_KEY])

    def test_topic_removed_from_config_drops_its_tracking(self):
        state = {
            "topic_health": {"fda": {"last_ok": _now_iso(1), "last_data": _now_iso(1)}},
            watchdog.DATA_BASELINE_KEY: {"fda": _days(1), "retired": _days(30)},
            watchdog.DATA_OUTAGES_KEY: {"retired": {
                "since": _days(30), "alerted": _days(20),
                "last_alert": _days(20), "reminders": 0}},
        }
        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(sent, [])  # no phantom recovery for an unconfigured topic
        self.assertNotIn("retired", state[watchdog.DATA_BASELINE_KEY])
        self.assertNotIn("retired", state[watchdog.DATA_OUTAGES_KEY])

    def test_invalid_day_values_are_skipped(self):
        for bad in ("soon", None, 0, -3):
            with self.subTest(days=bad):
                state = {"topic_health": {"fda": {"last_data": _days(400)}}}
                with _watchdog_cfg({"data_stale_days": {"fda": bad}}):
                    with capture_pushes() as sent:
                        watchdog.run(state)
                self.assertEqual(sent, [])

    def test_unparseable_baseline_restarts_the_clock(self):
        state = {"topic_health": {"fda": {}},
                 watchdog.DATA_BASELINE_KEY: {"fda": "not-a-date"}}
        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(sent, [])
        self.assertNotEqual(state[watchdog.DATA_BASELINE_KEY]["fda"], "not-a-date")


class ConfigTest(unittest.TestCase):
    def test_garbage_knobs_fall_back_to_defaults_instead_of_crashing(self):
        state = {"topic_health": {"fda": {
            "last_ok": _now_iso(2), "last_error": "boom",
            "last_error_ts": _now_iso(0.1)}}}
        with _watchdog_cfg({"alert_delay_hours": "soon", "reminder_hours": None}):
            with capture_pushes() as sent:
                watchdog.run(state)
        self.assertEqual(len(sent), 1)  # default delay 0 -> alerts immediately

    def test_legacy_stale_hours_still_honored_as_a_grace_period(self):
        # A monitors.json that predates the redesign must not suddenly alert on
        # a timeline its author never chose.
        state = {"topic_health": {"fda": {
            "last_ok": _now_iso(2), "last_error": "boom",
            "last_error_ts": _now_iso(0.1)}}}
        with _watchdog_cfg({"stale_hours": 48}):
            with capture_pushes() as sent:
                watchdog.run(state)
        self.assertEqual(sent, [])


class SwallowedFuelFailureTest(unittest.TestCase):
    """End-to-end proof of the topic health contract: a fuel source failure
    that fuel.run swallows internally (log + return state) must still reach the
    watchdog and alert, because main.py stamps last_ok only for a true ok report
    and a soft failure stays sticky across fuel's gated 3-hourly runs.
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

    def test_swallowed_fuel_failure_alerts_on_the_very_next_watchdog_run(self):
        last_good = _now_iso(72)
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

        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(len(sent), 1)
        self.assertIn("fuel", sent[0]["title"])
        self.assertIn("listing fetch failed", sent[0]["message"])
        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(sent, [])

    def test_recovery_closes_the_loop_and_rearms(self):
        state = {"topic_health": {"fuel": {"last_ok": _now_iso(72)}}}
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
        self.assertEqual(len(sent), 1)
        self.assertIn("recovered", sent[0]["title"])
        self.assertNotIn("fuel", state[watchdog.OUTAGES_KEY])


if __name__ == "__main__":
    unittest.main()


class EmptySourceTest(unittest.TestCase):
    """The 'reachable but returning nothing' check (watchdog.empty_days).

    The third failure class, and the one the August 2026 audit found live:
    `spending` had ingested nothing for three weeks while reporting ok on every
    run. The outage check saw no error because there was none, and the
    data-staleness check never started its clock because health.topic_status
    only stamps last_data when data_count > 0.
    """

    def test_quiet_topic_alerts_then_reminds_then_recovers(self):
        state = {"topic_health": {"spending": {"last_ok": _now_iso(1),
                                               "empty_since": _now_iso(24 * 30)}}}
        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(len(sent), 1)
        self.assertIn("answering empty", sent[0]["title"])
        self.assertIn("spending", state[watchdog.EMPTY_OUTAGES_KEY])

        with capture_pushes() as sent:  # inside the reminder window
            state = watchdog.run(state)
        self.assertEqual(sent, [])

        state[watchdog.EMPTY_OUTAGES_KEY]["spending"]["last_alert"] = _now_iso(24 * 30)
        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(len(sent), 1)
        self.assertIn("STILL answering empty", sent[0]["title"])

        # Items flow again -> main.py clears empty_since -> one all-clear.
        del state["topic_health"]["spending"]["empty_since"]
        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(len(sent), 1)
        self.assertIn("returning items again", sent[0]["title"])
        with capture_pushes() as sent:
            state = watchdog.run(state)
        self.assertEqual(sent, [])

    def test_recently_quiet_topic_is_silent(self):
        # Two days of no bank emails is a quiet fortnight, not a broken pipe.
        state = {"topic_health": {"spending": {"last_ok": _now_iso(1),
                                               "empty_since": _now_iso(48)}}}
        with capture_pushes() as sent:
            watchdog.run(state)
        self.assertEqual(sent, [])

    def test_topic_with_items_is_never_checked(self):
        # No empty_since stamp at all: main.py clears it whenever data_count>0.
        state = {"topic_health": {"spending": {"last_ok": _now_iso(1),
                                               "last_data_count": 3}}}
        with capture_pushes() as sent:
            watchdog.run(state)
        self.assertEqual(sent, [])

    def test_unconfigured_topic_is_never_checked(self):
        # Only topics listed in watchdog.empty_days are aged; `holidays` is
        # legitimately empty most of the year.
        state = {"topic_health": {"holidays": {"last_ok": _now_iso(1),
                                               "empty_since": _now_iso(24 * 300)}}}
        with capture_pushes() as sent:
            watchdog.run(state)
        self.assertEqual(sent, [])

    def test_outage_takes_precedence_and_both_can_fire(self):
        # A hard failure and a separate quiet topic are independent bundles.
        state = {"topic_health": {
            "fx": {"last_ok": _now_iso(30), "last_error": "boom",
                   "last_error_ts": _now_iso(1)},
            "spending": {"last_ok": _now_iso(1), "empty_since": _now_iso(24 * 30)},
        }}
        with capture_pushes() as sent:
            watchdog.run(state)
        titles = " | ".join(s["title"] for s in sent)
        self.assertIn("is failing", titles)
        self.assertIn("answering empty", titles)

    def test_empty_alert_survives_a_failed_push(self):
        # Delivery contract: no marker unless the push landed, so the next run
        # re-sends rather than losing the alert.
        state = {"topic_health": {"spending": {"last_ok": _now_iso(1),
                                               "empty_since": _now_iso(24 * 30)}}}
        with mock.patch.object(ntfy, "push", side_effect=RuntimeError("discord down")):
            with self.assertRaises(RuntimeError):
                watchdog.run(state)
        self.assertNotIn("alerted",
                         state.get(watchdog.EMPTY_OUTAGES_KEY, {}).get("spending", {}))
