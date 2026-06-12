"""Tests for the weekly gating of the games topic (notify_watcher.topics.games)."""
from __future__ import annotations

import datetime as _dt
import unittest
from unittest import mock

from notify_watcher.topics import games


class IsoWeekTest(unittest.TestCase):
    def test_label_format(self):
        self.assertEqual(games._iso_week(_dt.date(2026, 6, 8)), "2026-W24")

    def test_same_week_same_label(self):
        mon = _dt.date(2026, 6, 8)
        sun = _dt.date(2026, 6, 14)  # same ISO week
        self.assertEqual(games._iso_week(mon), games._iso_week(sun))

    def test_next_week_differs(self):
        self.assertNotEqual(
            games._iso_week(_dt.date(2026, 6, 8)),
            games._iso_week(_dt.date(2026, 6, 15)),
        )


class RunGateTest(unittest.TestCase):
    def test_skips_when_not_daily(self):
        with mock.patch.dict("os.environ", {"NOTIFY_DAILY": ""}, clear=False), \
             mock.patch.object(games, "_track_release_dates") as rd, \
             mock.patch.object(games, "_track_news") as nw:
            state = games.run({})
        rd.assert_not_called()
        nw.assert_not_called()
        self.assertNotIn(games.WEEK_STATE_KEY, state)

    def test_runs_once_per_week_and_stamps(self):
        week = games._iso_week(_dt.date.today())
        with mock.patch.dict("os.environ", {"NOTIFY_DAILY": "1"}, clear=False), \
             mock.patch.object(games, "_track_release_dates", side_effect=lambda s: (s, 1, 0)) as rd, \
             mock.patch.object(games, "_track_news", side_effect=lambda s: (s, 1, 0)) as nw:
            state = games.run({})
            self.assertEqual(state[games.WEEK_STATE_KEY], week)
            self.assertEqual(rd.call_count, 1)
            # Second run the same week is a no-op.
            games.run(state)
            self.assertEqual(rd.call_count, 1)
            self.assertEqual(nw.call_count, 1)

    def test_runs_again_when_week_rolls_over(self):
        with mock.patch.dict("os.environ", {"NOTIFY_DAILY": "1"}, clear=False), \
             mock.patch.object(games, "_track_release_dates", side_effect=lambda s: (s, 1, 0)) as rd, \
             mock.patch.object(games, "_track_news", side_effect=lambda s: (s, 1, 0)):
            state = {games.WEEK_STATE_KEY: "2000-W01"}  # stale week
            games.run(state)
        rd.assert_called_once()


class WeeklyRetryTest(unittest.TestCase):
    """run() stamps the week only when a configured check completed, so a
    total source outage retries on the next daily run (movie-countdown rule)."""

    def _run(self, releases, news_counts, state=None):
        with mock.patch.dict("os.environ", {"NOTIFY_DAILY": "1"}, clear=False), \
             mock.patch.object(games, "_track_release_dates",
                               side_effect=lambda s: (s, *releases)) as rd, \
             mock.patch.object(games, "_track_news",
                               side_effect=lambda s: (s, *news_counts)):
            out = games.run(state if state is not None else {})
        return out, rd

    def test_total_failure_leaves_week_unstamped(self):
        state, _ = self._run(releases=(0, 2), news_counts=(0, 3))
        self.assertNotIn(games.WEEK_STATE_KEY, state)
        # The unstamped week retries: the next daily run checks again.
        _, rd = self._run(releases=(0, 2), news_counts=(0, 3), state=state)
        rd.assert_called_once()

    def test_partial_success_stamps(self):
        week = games._iso_week(_dt.date.today())
        state, _ = self._run(releases=(0, 2), news_counts=(1, 1))
        self.assertEqual(state[games.WEEK_STATE_KEY], week)

    def test_nothing_configured_stamps(self):
        # No key + empty watchlist: explicitly nothing to do, don't re-run.
        week = games._iso_week(_dt.date.today())
        state, _ = self._run(releases=(0, 0), news_counts=(0, 0))
        self.assertEqual(state[games.WEEK_STATE_KEY], week)


class TrackerCountersTest(unittest.TestCase):
    """The per-check counters that feed the weekly stamp decision."""

    def test_release_dates_counts_completed_vs_raised(self):
        # "Good" completes (a no-match still means RAWG answered); "Bad" raises.
        def search(title, _key):
            if title == "Bad":
                raise OSError("down")
            return None
        with mock.patch.dict("os.environ", {"RAWG_API_KEY": "k"}, clear=False), \
             mock.patch.object(games.watchlist, "titles", return_value=["Good", "Bad"]), \
             mock.patch.object(games, "_search", side_effect=search):
            _, ok, failed = games._track_release_dates({})
        self.assertEqual((ok, failed), (1, 1))

    def test_release_dates_unconfigured_is_zero_zero(self):
        with mock.patch.dict("os.environ", {"RAWG_API_KEY": ""}, clear=False):
            _, ok, failed = games._track_release_dates({})
        self.assertEqual((ok, failed), (0, 0))

    def test_collect_news_flags_total_fetch_failure(self):
        with mock.patch.object(games, "_fetch_news", side_effect=OSError("down")):
            articles, fetched_any = games._collect_news("Some Game")
        self.assertEqual(articles, [])
        self.assertFalse(fetched_any)

    def test_collect_news_empty_feed_still_counts_as_fetched(self):
        with mock.patch.object(games, "_fetch_news", return_value=[]):
            articles, fetched_any = games._collect_news("Some Game")
        self.assertEqual(articles, [])
        self.assertTrue(fetched_any)

    def test_news_counts_total_fetch_failure_as_failed_check(self):
        with mock.patch.object(games.watchlist, "titles", return_value=["Some Game"]), \
             mock.patch.object(games, "_fetch_news", side_effect=OSError("down")):
            _, ok, failed = games._track_news({})
        self.assertEqual((ok, failed), (0, 1))

    def test_news_quiet_week_counts_as_success(self):
        with mock.patch.object(games.watchlist, "titles", return_value=["Some Game"]), \
             mock.patch.object(games, "_fetch_news", return_value=[]):
            _, ok, failed = games._track_news({})
        self.assertEqual((ok, failed), (1, 0))


if __name__ == "__main__":
    unittest.main()
