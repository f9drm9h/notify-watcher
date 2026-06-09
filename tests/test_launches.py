"""Tests for the rocket-launch watcher (notify_watcher.topics.launches)."""
from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

from notify_watcher import ids
from notify_watcher.topics import launches
from tests._util import capture_pushes

NOW = dt.datetime(2026, 6, 8, 12, 0, tzinfo=dt.timezone.utc)
CFG = {"imminent_hours": 24, "skip_keywords": ["Starlink"]}


def _r(rid, name, net):
    return {"id": rid, "name": name, "net": net, "vidURLs": [{"url": f"http://watch/{rid}"}]}


class SelectTest(unittest.TestCase):
    def test_imminent_non_starlink_selected(self):
        rows = launches._select([_r("1", "Falcon 9 | Crew-12", "2026-06-08T20:00:00Z")], NOW, CFG)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "1")

    def test_starlink_skipped(self):
        rows = launches._select([_r("2", "Falcon 9 | Starlink Group 10-35", "2026-06-08T20:00:00Z")], NOW, CFG)
        self.assertEqual(rows, [])

    def test_far_future_excluded(self):
        rows = launches._select([_r("3", "Ariane 6", "2026-06-20T20:00:00Z")], NOW, CFG)
        self.assertEqual(rows, [])

    def test_past_excluded(self):
        rows = launches._select([_r("4", "Vulcan", "2026-06-08T06:00:00Z")], NOW, CFG)
        self.assertEqual(rows, [])

    def test_bad_net_skipped(self):
        rows = launches._select([_r("5", "Mystery", "")], NOW, CFG)
        self.assertEqual(rows, [])


class SteadyStateRoutingTest(unittest.TestCase):
    """A fresh imminent launch now pushes through events.emit; with no `priority`
    section the engine is OFF and the legacy default-priority push is unchanged."""

    def _run(self, results, state):
        resp = mock.Mock()
        resp.json.return_value = {"results": results}
        resp.raise_for_status.return_value = None
        with mock.patch.object(launches.requests, "get", return_value=resp), \
             mock.patch.object(launches.config, "section",
                               side_effect=lambda n: CFG if n == "launches" else {}), \
             capture_pushes() as sent:
            state = launches.run(state)
        return state, sent

    @staticmethod
    def _soon():
        # run() compares against the real wall clock, so anchor to now (+3h).
        return (dt.datetime.now(dt.timezone.utc)
                + dt.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_seeded_then_new_launch_pushes(self):
        # Pre-seed so this is a steady-state run, not first-run seeding.
        state, sent = self._run([_r("L1", "Falcon 9 | Crew-12", self._soon())],
                                {"launch_seen_ids": []})
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["title"], "Rocket launch")
        self.assertEqual(sent[0]["priority"], "default")
        self.assertIn(ids.short("L1"), state["launch_seen_ids"])

    def test_already_seen_launch_does_not_repush(self):
        _, sent = self._run([_r("L1", "Falcon 9 | Crew-12", self._soon())],
                            {"launch_seen_ids": [ids.short("L1")]})
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
