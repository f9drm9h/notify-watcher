"""Tests for the shared collector engine (notify_watcher.monitor)."""
from __future__ import annotations

import unittest
from unittest import mock

from notify_watcher import digest, ids, monitor
from tests._util import capture_pushes

SCORING = {
    "source_weights": {"regulatory": 5, "trade": 1},
    "signal_bonuses": {"action": {"weight": 3, "terms": ["approves"]}},
    "noise_penalties": {},
    "thresholds": {"breakthrough": 8, "high": 6, "moderate": 4},
}
DIGEST = {"max_buffer": 50, "max_items_in_message": 25}


def _item(iid, title, weight="trade"):
    return {"id": iid, "title": title, "url": f"http://x/{iid}", "source": "Src", "weight": weight}


class RunSourceTest(unittest.TestCase):
    def setUp(self):
        # Pin the Personal Priority Engine OFF so these tests exercise
        # run_source's LEGACY tier routing. monitors.json now ships a `priority`
        # section that emit would otherwise read (via config.section) and apply.
        p = mock.patch("notify_watcher.config.section", return_value={})
        p.start()
        self.addCleanup(p.stop)

    def _run(self, state, items):
        return monitor.run_source(
            state, state_key="k", items=items, default_weight_key="trade",
            keywords=[], scoring_cfg=SCORING, digest_cfg=DIGEST, cap=100,
            live_title_prefix="Test",
        )

    def test_first_run_seeds_silently(self):
        state: dict = {}
        with capture_pushes() as sent:
            state = self._run(state, [_item("a", "approves drug", "regulatory")])
        self.assertEqual(sent, [])
        self.assertEqual(state["k"], [ids.short("a")])  # stored hashed, not raw

    def test_routes_tiers_and_records_all(self):
        state = {"k": []}
        items = [
            _item("hi", "approves drug", "regulatory"),  # 5+3=8 -> breakthrough live
            _item("mod", "minor notice", "regulatory"),  # 5 -> moderate digest
            _item("low", "routine note", "trade"),       # 1 -> minor drop
        ]
        with capture_pushes() as sent:
            state = self._run(state, items)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["priority"], "urgent")  # breakthrough
        self.assertEqual(len(state.get(digest.BUFFER_KEY, [])), 1)
        # all recorded seen, stored as hashes
        self.assertEqual(set(state["k"]), {ids.short(x) for x in ("hi", "mod", "low")})

    def test_legacy_raw_ids_migrate_without_refiring(self):
        # A pre-migration seen-list holds the raw id; the same item must be
        # recognised (via its hash) and NOT re-alerted, then stored hashed.
        state = {"k": ["hi"]}  # legacy raw id
        with capture_pushes() as sent:
            state = self._run(state, [_item("hi", "approves drug", "regulatory")])
        self.assertEqual(sent, [])
        self.assertEqual(state["k"], [ids.short("hi")])

    def test_dedup_holds_on_rerun(self):
        state = {"k": []}
        items = [_item("hi", "approves drug", "regulatory")]
        with capture_pushes() as sent:
            state = self._run(state, items)
            state = self._run(state, items)
        self.assertEqual(len(sent), 1)

    def test_seen_list_is_capped(self):
        state = {"k": []}
        items = [_item(f"i{n}", "routine", "trade") for n in range(150)]
        state = monitor.run_source(
            state, state_key="k", items=items, default_weight_key="trade",
            keywords=[], scoring_cfg=SCORING, digest_cfg=DIGEST, cap=100,
            live_title_prefix="Test",
        )
        self.assertEqual(len(state["k"]), 100)


if __name__ == "__main__":
    unittest.main()
