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


class PushDeliveryTest(unittest.TestCase):
    """push() gates on quiet hours, then hands off to the Discord transport."""

    def test_push_drops_when_quiet(self):
        with mock.patch.object(ntfy, "_is_quiet_now", return_value=True), \
             mock.patch.object(ntfy.discord_delivery, "send") as send:
            ntfy.push(title="hi", message="there", priority="low", topic="fx")
        send.assert_not_called()

    def test_push_delivers_when_not_quiet(self):
        with mock.patch.object(ntfy, "_is_quiet_now", return_value=False), \
             mock.patch.object(ntfy.discord_delivery, "send") as send:
            ntfy.push(title="hi", message="there", priority="low",
                      topic="fx", severity="moderate")
        send.assert_called_once()
        # topic/title/message are positional; routing hints are keyword.
        self.assertEqual(send.call_args.args[0], "fx")
        self.assertEqual(send.call_args.kwargs.get("severity"), "moderate")

    def test_attach_url_forwarded_to_transport(self):
        with mock.patch.object(ntfy, "_is_quiet_now", return_value=False), \
             mock.patch.object(ntfy.discord_delivery, "send") as send:
            ntfy.push(title="hi", message="there", topic="apod",
                      attach_url="https://example.com/pic.jpg")
        self.assertEqual(send.call_args.kwargs.get("attach_url"),
                         "https://example.com/pic.jpg")

    _CTRL_ENV = {"DISCORD_TOKEN": "tok", "DISCORD_CONTROL_CHANNEL": "555"}

    def test_actions_become_components_when_control_loop_on(self):
        # Reply-button descriptors are rendered as native Discord components and
        # forwarded as `components` (never as a raw `actions` kwarg).
        with mock.patch.dict("os.environ", self._CTRL_ENV, clear=False), \
             mock.patch.object(ntfy, "_is_quiet_now", return_value=False), \
             mock.patch.object(ntfy.discord_delivery, "send") as send:
            ntfy.push(title="hi", message="there", topic="fx",
                      actions=[{"label": "Mute 24h", "command": "MUTE:fx:24"}])
        send.assert_called_once()
        self.assertNotIn("actions", send.call_args.kwargs)
        rows = send.call_args.kwargs.get("components")
        self.assertEqual(rows[0]["components"][0]["custom_id"], "nw|MUTE:fx:24")

    def test_actions_render_nothing_when_control_loop_off(self):
        with mock.patch.dict("os.environ",
                             {"DISCORD_TOKEN": "", "DISCORD_CONTROL_CHANNEL": ""},
                             clear=False), \
             mock.patch.object(ntfy, "_is_quiet_now", return_value=False), \
             mock.patch.object(ntfy.discord_delivery, "send") as send:
            ntfy.push(title="hi", message="there", topic="fx",
                      actions=[{"label": "Mute 24h", "command": "MUTE:fx:24"}])
        send.assert_called_once()
        self.assertIsNone(send.call_args.kwargs.get("components"))


if __name__ == "__main__":
    unittest.main()
