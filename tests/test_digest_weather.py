"""Tests for the digest's morning weather line (notify_watcher.topics.digest_topic).

_weather_line is exercised against mocked HTTP responses — no network. The
fetch must degrade gracefully: missing fields are left out of the line, and any
error yields None so the digest itself is never blocked on weather.
"""
from __future__ import annotations

import unittest
from unittest import mock

from notify_watcher import digest
from notify_watcher.topics import digest_topic
from tests._util import capture_pushes

LOCATION = {"latitude": 18.52, "longitude": -69.82}

PAYLOAD = {
    "current": {"temperature_2m": 31.2},
    "daily": {
        "precipitation_probability_max": [20],
        "uv_index_max": [9.05],
    },
}


class _Resp:
    def __init__(self, payload: dict, status_ok: bool = True):
        self._payload = payload
        self._status_ok = status_ok

    def raise_for_status(self):
        if not self._status_ok:
            raise RuntimeError("HTTP 500")

    def json(self):
        return self._payload


def _patched(payload=PAYLOAD, location=LOCATION, status_ok=True, get=None):
    """Patch config + requests inside digest_topic; returns the context managers."""
    sections = {"location": location or {}}
    cfg = mock.patch.object(
        digest_topic.config, "section",
        side_effect=lambda name: sections.get(name, {}),
    )
    http = mock.patch.object(
        digest_topic.requests, "get",
        side_effect=get or (lambda *a, **kw: _Resp(payload, status_ok)),
    )
    return cfg, http


class WeatherLineTest(unittest.TestCase):
    def test_full_payload_formats_one_liner(self):
        cfg, http = _patched()
        with cfg, http:
            self.assertEqual(digest_topic._weather_line({}),
                             "Today: 31 °C, rain 20%, UV 9")

    def test_missing_fields_are_left_out(self):
        payload = {"current": {"temperature_2m": 28.6}, "daily": {}}
        cfg, http = _patched(payload=payload)
        with cfg, http:
            self.assertEqual(digest_topic._weather_line({}), "Today: 29 °C")

    def test_empty_payload_returns_none(self):
        cfg, http = _patched(payload={})
        with cfg, http:
            self.assertIsNone(digest_topic._weather_line({}))

    def test_http_error_returns_none(self):
        cfg, http = _patched(status_ok=False)
        with cfg, http:
            self.assertIsNone(digest_topic._weather_line({}))

    def test_network_exception_returns_none(self):
        def _boom(*a, **kw):
            raise OSError("connection refused")
        cfg, http = _patched(get=_boom)
        with cfg, http:
            self.assertIsNone(digest_topic._weather_line({}))

    def test_no_location_returns_none_without_fetching(self):
        calls = []

        def _record(*a, **kw):
            calls.append(1)
            return _Resp(PAYLOAD)
        cfg, http = _patched(location={}, get=_record)
        with cfg, http:
            self.assertIsNone(digest_topic._weather_line({}))
        self.assertEqual(calls, [])

    def test_non_numeric_values_are_skipped(self):
        payload = {
            "current": {"temperature_2m": "n/a"},
            "daily": {"precipitation_probability_max": [None],
                      "uv_index_max": [7.8]},
        }
        cfg, http = _patched(payload=payload)
        with cfg, http:
            self.assertEqual(digest_topic._weather_line({}), "Today: UV 8")


class FlushHeaderTest(unittest.TestCase):
    def test_header_is_first_line_of_digest(self):
        state: dict = {}
        digest.add(state, {"title": "headline", "source": "Energy"}, {})
        with capture_pushes() as sent:
            self.assertTrue(digest.flush(state, {}, header="Today: 31 °C, rain 20%, UV 9"))
        body = sent[0]["message"]
        self.assertTrue(body.startswith("Today: 31 °C, rain 20%, UV 9\n"))
        self.assertIn("ENERGY", body)

    def test_no_header_leaves_digest_unchanged(self):
        state: dict = {}
        digest.add(state, {"title": "headline", "source": "Energy"}, {})
        with capture_pushes() as sent:
            self.assertTrue(digest.flush(state, {}, header=None))
        self.assertTrue(sent[0]["message"].startswith("ENERGY"))


class FollowButtonTest(unittest.TestCase):
    BUF = [
        {"title": "a", "score": 10, "topic": "movies"},
        {"title": "b", "score": 80, "topic": "games"},
        {"title": "legacy item without topic", "score": 99},
    ]

    def _action(self, state, digest_cfg=None):
        with mock.patch.dict("os.environ", {"NTFY_CONTROL_TOPIC": "ctl"}), \
                mock.patch.object(digest_topic.config, "section",
                                  return_value=digest_cfg or {}):
            return digest_topic._follow_action(state)

    def test_targets_the_top_scored_topic(self):
        action = self._action({"digest_buffer": list(self.BUF)})
        self.assertEqual(action["label"], "Follow games 3d")
        self.assertEqual(action["body"], "FOLLOW:games:72")

    def test_disabled_by_config(self):
        self.assertIsNone(self._action({"digest_buffer": list(self.BUF)},
                                       {"follow_button": False}))

    def test_no_topic_carrying_items_means_no_button(self):
        self.assertIsNone(self._action(
            {"digest_buffer": [{"title": "legacy", "score": 99}]}))
        self.assertIsNone(self._action({}))


if __name__ == "__main__":
    unittest.main()
