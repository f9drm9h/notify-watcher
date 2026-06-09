"""Tests for the daily "Energy & Electricity Learning" topic.

Pure helpers (channel rotation, unseen-first pick + reset, the 24h news pick, the
news-slot gate, composition) are tested directly with injected dates/state, no network.
The run() tests drive the daily gate, idempotency, the curated default path, and the
occasional news path, asserting on what WOULD be pushed (summarize patched to force the
verbatim/headline fallback so there is no API dependency).
"""
from __future__ import annotations

import datetime as _dt
import unittest
from unittest import mock

from notify_watcher.topics import energy_learn as el
from tests._util import capture_pushes


def _iso(dt: _dt.datetime) -> str:
    return dt.isoformat()


class CuratedChannelTest(unittest.TestCase):
    def test_rotation_cycles_all_four_channels(self):
        # Four consecutive days must touch four distinct channels (a permutation
        # of the channel order), and the same day is deterministic.
        base = _dt.date(2026, 1, 1)
        keys = [el._curated_channel(base + _dt.timedelta(days=i))[0] for i in range(4)]
        self.assertEqual(set(keys), {c[0] for c in el.CHANNELS})
        self.assertEqual(el._curated_channel(base)[0], el._curated_channel(base)[0])

    def test_each_channel_loads_real_content(self):
        # Every channel's data file is present and non-empty with required fields.
        for ckey, _label, filename in el.CHANNELS:
            items = el.kb.load(el.kb.DATA_DIR / filename, field="what")
            self.assertTrue(items, f"{filename} should have entries")
            self.assertTrue(all(e.get("id") and e.get("why") and e.get("care")
                                for e in items))


class PickUnseenTest(unittest.TestCase):
    ITEMS = [{"id": "a", "what": "x"}, {"id": "b", "what": "y"}, {"id": "c", "what": "z"}]

    def test_no_repeat_until_exhausted_then_reset(self):
        seen: list = []
        picked = []
        for i in range(len(self.ITEMS)):
            entry, seen = el._pick_unseen(self.ITEMS, seen, _dt.date(2026, 1, 1) + _dt.timedelta(days=i))
            picked.append(entry["id"])
        # one full pass visits every id exactly once
        self.assertEqual(set(picked), {"a", "b", "c"})
        self.assertEqual(len(set(picked)), 3)
        # the next pick triggers a reset (seen was full) and still returns an entry
        entry, seen = el._pick_unseen(self.ITEMS, seen, _dt.date(2026, 1, 5))
        self.assertIn(entry["id"], {"a", "b", "c"})
        self.assertEqual(seen, [entry["id"]])  # seen-list reset to just the new pick

    def test_seen_list_capped_to_channel_size(self):
        _e, seen = el._pick_unseen(self.ITEMS, ["a", "b", "c"], _dt.date(2026, 2, 2))
        self.assertLessEqual(len(seen), len(self.ITEMS))


class RecentEnergyTest(unittest.TestCase):
    def setUp(self):
        self.now = _dt.datetime(2026, 6, 9, 18, 0, tzinfo=_dt.timezone.utc)

    def _log(self):
        return [
            {"topic": "energy", "title": "low", "score": 3, "ts": _iso(self.now - _dt.timedelta(hours=2))},
            {"topic": "energy", "title": "high", "score": 9, "ts": _iso(self.now - _dt.timedelta(hours=5))},
            {"topic": "energy", "title": "stale", "score": 99, "ts": _iso(self.now - _dt.timedelta(hours=30))},
            {"topic": "games", "title": "other", "score": 88, "ts": _iso(self.now)},
        ]

    def test_picks_highest_recent_energy_only(self):
        top = el._top_recent_energy(self._log(), self.now)
        self.assertEqual(top["title"], "high")  # not the stale 99 nor the games 88

    def test_empty_log(self):
        self.assertIsNone(el._top_recent_energy([], self.now))


class NewsGateTest(unittest.TestCase):
    CFG = {"min_news_score": 6, "news_min_gap_days": 5}
    TODAY = _dt.date(2026, 6, 9)

    def test_requires_minimum_score(self):
        self.assertFalse(el._should_use_news({}, {"score": 3}, self.TODAY, self.CFG))
        self.assertTrue(el._should_use_news({}, {"score": 6}, self.TODAY, self.CFG))

    def test_respects_gap_since_last_news(self):
        recent = {el.LAST_NEWS_KEY: (self.TODAY - _dt.timedelta(days=2)).isoformat()}
        self.assertFalse(el._should_use_news(recent, {"score": 9}, self.TODAY, self.CFG))
        old = {el.LAST_NEWS_KEY: (self.TODAY - _dt.timedelta(days=10)).isoformat()}
        self.assertTrue(el._should_use_news(old, {"score": 9}, self.TODAY, self.CFG))

    def test_no_story_means_no_news(self):
        self.assertFalse(el._should_use_news({}, None, self.TODAY, self.CFG))


class ComposeTest(unittest.TestCase):
    def test_curated_body_has_all_three_parts_and_source(self):
        body = el._compose_curated({"what": "W", "why": "Y", "care": "C", "src": "S"})
        self.assertIn("What: W", body)
        self.assertIn("Why it matters: Y", body)
        self.assertIn("Why you should care: C", body)
        self.assertIn("(Source: S)", body)


class RunTest(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict("os.environ", {"NOTIFY_DAILY": "1"})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.today = _dt.date.today().isoformat()

    def test_curated_default_pushes_and_stamps(self):
        # Empty event_log -> no news -> curated "Today's spark" push.
        with capture_pushes() as sent:
            state = el.run({})
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0]["title"].startswith("⚡ Today's spark"))
        self.assertIn("Why it matters:", sent[0]["message"])
        self.assertEqual(state[el.LAST_SENT_KEY], self.today)
        self.assertTrue(state[el.SEEN_KEY])  # recorded a delivered id

    def test_news_slot_when_fresh_high_story(self):
        now = _dt.datetime.now(_dt.timezone.utc)
        state = {"event_log": [{"topic": "energy", "title": "Grid battery milestone",
                                "source": "EIA", "score": 9, "url": "http://x",
                                "ts": now.isoformat(), "action": "digest"}]}
        with mock.patch.object(el.summarize, "one_line", return_value=None), \
             capture_pushes() as sent:
            out = el.run(state)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["title"], "⚡ Energy now")
        self.assertIn('Headline: "Grid battery milestone"', sent[0]["message"])
        self.assertEqual(out[el.LAST_NEWS_KEY], self.today)
        self.assertEqual(out[el.LAST_SENT_KEY], self.today)

    def test_idempotent_per_day(self):
        with capture_pushes() as sent:
            state = el.run({})
            el.run(state)  # second run same day
        self.assertEqual(len(sent), 1)

    def test_skips_when_not_daily(self):
        with mock.patch.dict("os.environ", {"NOTIFY_DAILY": ""}, clear=False), \
             capture_pushes() as sent:
            state = el.run({})
        self.assertEqual(sent, [])
        self.assertNotIn(el.LAST_SENT_KEY, state)

    def test_degrades_when_no_curated_content(self):
        # All channels empty -> clean skip, no push, no stamp (retries next run).
        with mock.patch.object(el.kb, "load", return_value=[]), \
             capture_pushes() as sent:
            state = el.run({})
        self.assertEqual(sent, [])
        self.assertNotIn(el.LAST_SENT_KEY, state)


if __name__ == "__main__":
    unittest.main()
