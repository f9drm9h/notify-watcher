"""Tests for the rough-seas watcher (notify_watcher.topics.marine).

marine.run() is thin plumbing around one threshold, so these tests drive run()
itself with a faked Open-Meteo response. With the real monitors.json priority
rules, a marine event (score 38) routes to the DAILY DIGEST, not a live push —
the assertions pin that routing too.
"""
from __future__ import annotations

import unittest
from unittest import mock

from notify_watcher import digest
from notify_watcher.topics import marine
from tests._util import capture_pushes


class _FakeResponse:
    def __init__(self, wave):
        self._wave = wave

    def raise_for_status(self):
        pass

    def json(self):
        return {"daily": {"wave_height_max": [self._wave]}}


class RunTest(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict("os.environ", {"NOTIFY_DAILY": "1"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def _run(self, state, wave):
        with mock.patch.object(marine.requests, "get",
                               return_value=_FakeResponse(wave)), \
             capture_pushes() as sent:
            state = marine.run(state)
        return state, sent

    def test_rough_day_lands_in_the_digest_once(self):
        state, sent = self._run({}, wave=2.5)
        self.assertEqual(sent, [])  # score 38 -> digest, not a live push
        buf = state.get(digest.BUFFER_KEY) or []
        self.assertEqual(len(buf), 1)
        self.assertEqual(buf[0]["title"], "Rough seas today")
        self.assertIn("2.5 m", buf[0]["detail"])
        # Same-day guard: a re-run never double-sends.
        state, _ = self._run(state, wave=2.5)
        self.assertEqual(len(state.get(digest.BUFFER_KEY) or []), 1)

    def test_calm_day_is_silent(self):
        state, sent = self._run({}, wave=1.0)
        self.assertEqual(sent, [])
        self.assertEqual(state.get(digest.BUFFER_KEY) or [], [])
        self.assertNotIn(marine.STATE_KEY, state)  # no stamp: nothing was sent

    def test_fetch_failure_is_graceful(self):
        with mock.patch.object(marine.requests, "get",
                               side_effect=RuntimeError("down")), \
             capture_pushes() as sent:
            state = marine.run({})
        self.assertEqual(sent, [])
        self.assertEqual(state, {})

    def test_skips_outside_the_daily_run(self):
        with mock.patch.dict("os.environ", {"NOTIFY_DAILY": ""}), \
             capture_pushes() as sent:
            state = marine.run({})
        self.assertEqual(sent, [])
        self.assertEqual(state, {})


if __name__ == "__main__":
    unittest.main()
