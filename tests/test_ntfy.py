"""Tests for ntfy quiet-hours suppression (notify_watcher.ntfy)."""
from __future__ import annotations

import datetime as _dt
import unittest
from unittest import mock

from notify_watcher import ntfy

UTC = _dt.timezone.utc


def _utc(hour: int, minute: int = 0) -> _dt.datetime:
    return _dt.datetime(2026, 6, 8, hour, minute, tzinfo=UTC)


# A window of 22:00-07:00 local with DR's -4 offset => 02:00-11:00 UTC.
ON = {"enabled": True, "start": "22:00", "end": "07:00", "utc_offset_hours": -4}


class ParseTest(unittest.TestCase):
    def test_parse_valid(self):
        self.assertEqual(ntfy._parse_hhmm("07:30"), 7 * 60 + 30)

    def test_parse_rejects_garbage(self):
        for bad in ["7", "7:60", "24:00", "ab:cd", 700, None]:
            self.assertIsNone(ntfy._parse_hhmm(bad))


class WindowTest(unittest.TestCase):
    def test_non_wrapping(self):
        self.assertTrue(ntfy._in_window(9 * 60, 8 * 60, 17 * 60))
        self.assertFalse(ntfy._in_window(17 * 60, 8 * 60, 17 * 60))  # end exclusive

    def test_wrapping_past_midnight(self):
        start, end = 22 * 60, 7 * 60
        self.assertTrue(ntfy._in_window(23 * 60, start, end))
        self.assertTrue(ntfy._in_window(3 * 60, start, end))
        self.assertFalse(ntfy._in_window(12 * 60, start, end))

    def test_zero_width_suppresses_nothing(self):
        self.assertFalse(ntfy._in_window(5 * 60, 5 * 60, 5 * 60))


class SuppressTest(unittest.TestCase):
    def test_disabled_never_suppresses(self):
        cfg = dict(ON, enabled=False)
        self.assertFalse(ntfy._quiet_suppresses("low", cfg, _utc(4)))

    def test_low_suppressed_inside_window(self):
        # 04:00 UTC == 00:00 local -> inside 22:00-07:00.
        self.assertTrue(ntfy._quiet_suppresses("low", ON, _utc(4)))
        self.assertTrue(ntfy._quiet_suppresses("default", ON, _utc(4)))
        self.assertTrue(ntfy._quiet_suppresses(None, ON, _utc(4)))

    def test_high_and_urgent_always_ring(self):
        self.assertFalse(ntfy._quiet_suppresses("high", ON, _utc(4)))
        self.assertFalse(ntfy._quiet_suppresses("urgent", ON, _utc(4)))

    def test_not_suppressed_outside_window(self):
        # 15:00 UTC == 11:00 local -> outside the window.
        self.assertFalse(ntfy._quiet_suppresses("low", ON, _utc(15)))

    def test_malformed_config_fails_open(self):
        self.assertFalse(ntfy._quiet_suppresses("low", {"enabled": True}, _utc(4)))
        self.assertFalse(
            ntfy._quiet_suppresses("low", dict(ON, utc_offset_hours="x"), _utc(4))
        )


class IsQuietNowTest(unittest.TestCase):
    def test_test_push_bypasses_suppression(self):
        with mock.patch.dict("os.environ", {"NOTIFY_TEST_PUSH": "1"}, clear=False), \
             mock.patch.object(ntfy.config, "section", return_value=ON):
            self.assertFalse(ntfy._is_quiet_now("low"))

    def test_config_error_fails_open(self):
        with mock.patch.dict("os.environ", {"NOTIFY_TEST_PUSH": ""}, clear=False), \
             mock.patch.object(ntfy.config, "section", side_effect=RuntimeError("boom")):
            self.assertFalse(ntfy._is_quiet_now("low"))


class PushIntegrationTest(unittest.TestCase):
    def test_push_drops_when_quiet(self):
        with mock.patch.object(ntfy, "_is_quiet_now", return_value=True), \
             mock.patch.object(ntfy.requests, "post") as post:
            ntfy.push(title="hi", message="there", priority="low")
        post.assert_not_called()

    def test_push_sends_when_not_quiet(self):
        with mock.patch.object(ntfy, "_is_quiet_now", return_value=False), \
             mock.patch.dict("os.environ", {"NTFY_TOPIC": "t"}, clear=False), \
             mock.patch.object(ntfy.requests, "post") as post:
            ntfy.push(title="hi", message="there", priority="low")
        post.assert_called_once()

    def test_attach_url_sets_attach_header(self):
        with mock.patch.object(ntfy, "_is_quiet_now", return_value=False), \
             mock.patch.dict("os.environ", {"NTFY_TOPIC": "t"}, clear=False), \
             mock.patch.object(ntfy.requests, "post") as post:
            ntfy.push(title="hi", message="there",
                      attach_url="https://example.com/pic.jpg")
            ntfy.push(title="hi", message="there")  # no attach -> no header
        first, second = post.call_args_list
        self.assertEqual(first.kwargs["headers"]["Attach"],
                         "https://example.com/pic.jpg")
        self.assertNotIn("Attach", second.kwargs["headers"])


if __name__ == "__main__":
    unittest.main()
