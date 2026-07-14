"""Tests for the consolidated spark topic (notify_watcher.topics.spark).

spark replaces the old wikiquote / knowledge-story / health-tip channels with
one topic that fires once per 6-hour epoch window (the same
``time.time() // window_secs`` math the watch workflow uses for its 3-hour
full-sweep gate, with a 21600s window) and rotates the content type
quote -> fact -> health -> repeat. The content providers themselves are
covered by tests/test_wikiquote.py, tests/test_learn_knowledge.py and
tests/test_health_tip.py; here they are patched out so only the gate and the
rotation are under test.
"""
from __future__ import annotations

import unittest
from unittest import mock

from notify_watcher.topics import health_tip, learn, spark, wikiquote

# Any timestamp works; windows are counted from the epoch.
T0 = 1_780_000_000.0 - (1_780_000_000.0 % spark.SPARK_WINDOW_SECS)


class WindowTest(unittest.TestCase):
    """The 6-hour epoch-window id that guards double-sends."""

    def test_window_is_epoch_seconds_over_21600(self):
        self.assertEqual(spark.SPARK_WINDOW_SECS, 21600)
        self.assertEqual(spark._window(T0), int(T0 // 21600))

    def test_drift_inside_a_window_keeps_the_id(self):
        self.assertEqual(spark._window(T0), spark._window(T0 + 21599))
        self.assertNotEqual(spark._window(T0), spark._window(T0 + 21600))


class RotationTest(unittest.TestCase):
    """One send per window, rotating quote -> fact -> health -> repeat."""

    def _patch_providers(self, *, quote=True, fact=True, health=True):
        calls: list[str] = []

        def mk(kind: str, result: bool):
            def _send(state):
                calls.append(kind)
                return result
            return _send

        for module, attr, kind, result in (
            (wikiquote, "send_quote", "quote", quote),
            (learn, "send_knowledge", "fact", fact),
            (health_tip, "send_tip", "health", health),
        ):
            patcher = mock.patch.object(module, attr, side_effect=mk(kind, result))
            patcher.start()
            self.addCleanup(patcher.stop)
        return calls

    def test_successive_windows_rotate_through_the_sequence(self):
        calls = self._patch_providers()
        state: dict = {}
        for i in range(6):
            state = spark._run(state, T0 + i * spark.SPARK_WINDOW_SECS)
        self.assertEqual(calls, ["quote", "fact", "health", "quote", "fact", "health"])
        self.assertEqual(state[spark.SPARK_KIND_KEY], "quote")

    def test_rerun_inside_a_window_is_a_noop(self):
        calls = self._patch_providers()
        state = spark._run({}, T0)
        state = spark._run(state, T0 + 900)  # 15 minutes later, same window
        self.assertEqual(calls, ["quote"], "a re-run inside the window must not resend")
        self.assertEqual(state[spark.SPARK_SENT_KEY], spark._window(T0))

    def test_failed_send_leaves_window_unstamped_and_retries_same_kind(self):
        calls = self._patch_providers(quote=False)
        state = spark._run({}, T0)
        self.assertNotIn(spark.SPARK_SENT_KEY, state)
        self.assertNotIn(spark.SPARK_KIND_KEY, state)
        # The next run (even in the same window) retries the same type.
        spark._run(state, T0 + 900)
        self.assertEqual(calls, ["quote", "quote"])

    def test_failure_mid_rotation_does_not_advance_the_pointer(self):
        calls = self._patch_providers(health=False)
        state: dict = {}
        for i in range(4):
            state = spark._run(state, T0 + i * spark.SPARK_WINDOW_SECS)
        # health failed on window 2 and was retried (not skipped) on window 3.
        self.assertEqual(calls, ["quote", "fact", "health", "health"])
        self.assertEqual(state[spark.SPARK_KIND_KEY], "health")

    def test_unknown_state_kind_falls_back_to_first_in_sequence(self):
        calls = self._patch_providers()
        spark._run({spark.SPARK_KIND_KEY: "retired_kind"}, T0)
        self.assertEqual(calls, ["quote"])

    def test_run_entry_point_uses_the_real_clock(self):
        calls = self._patch_providers()
        before = spark._window()
        state = spark.run({})
        self.assertEqual(calls, ["quote"])
        self.assertIn(state[spark.SPARK_SENT_KEY], (before, spark._window()))
        self.assertEqual(state[spark.SPARK_KIND_KEY], "fact")


if __name__ == "__main__":
    unittest.main()
