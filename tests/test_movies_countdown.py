"""Tests for the weekly movie release countdown (notify_watcher.topics.movies).

Pure stdlib unittest, no network: TMDb lookups are patched out and pushes are
captured via tests/_util.capture_pushes. The pure line builder is tested
directly; the weekly gate (NOTIFY_DAILY + per-ISO-week stamp) is exercised
through _weekly_countdown with an injected `today`.
"""
from __future__ import annotations

import datetime as _dt
import unittest
from unittest import mock

from notify_watcher.topics import movies
from tests._util import capture_pushes

MONDAY = _dt.date(2026, 6, 8)


def _env(**overrides):
    base = {"NOTIFY_DAILY": "", "TMDB_API_KEY": ""}
    base.update(overrides)
    return mock.patch.dict("os.environ", base)


class CountdownLinesTest(unittest.TestCase):
    def test_counts_down_films_inside_the_window(self):
        films = [
            ("Avengers: Doomsday", "2026-06-26"),   # 18 days out
            ("The Odyssey", "2026-07-17"),           # 39 days out
        ]
        self.assertEqual(movies._countdown_lines(films, MONDAY), [
            "Avengers: Doomsday releases in 18 days",
            "The Odyssey releases in 39 days",
        ])

    def test_sorted_soonest_first(self):
        films = [("Far", "2026-07-17"), ("Near", "2026-06-10")]
        lines = movies._countdown_lines(films, MONDAY)
        self.assertEqual(lines[0], "Near releases in 2 days")
        self.assertEqual(lines[1], "Far releases in 39 days")

    def test_today_and_tomorrow_phrasing(self):
        films = [("Today Film", "2026-06-08"), ("Tomorrow Film", "2026-06-09")]
        self.assertEqual(movies._countdown_lines(films, MONDAY), [
            "Today Film releases today",
            "Tomorrow Film releases tomorrow",
        ])

    def test_unconfirmed_dates_never_count_down(self):
        films = [("TBA Film", "TBA"), ("Empty", ""), ("Junk", "soon™")]
        self.assertEqual(movies._countdown_lines(films, MONDAY), [])

    def test_outside_window_and_already_released_excluded(self):
        films = [
            ("Too Far", (MONDAY + _dt.timedelta(days=61)).isoformat()),
            ("Released", (MONDAY - _dt.timedelta(days=1)).isoformat()),
            ("Edge", (MONDAY + _dt.timedelta(days=60)).isoformat()),
        ]
        self.assertEqual(movies._countdown_lines(films, MONDAY),
                         ["Edge releases in 60 days"])


class WeeklyCountdownTest(unittest.TestCase):
    def _films(self, *pairs):
        """Patch the TMDb search to resolve titles from a canned table."""
        table = {t: {"title": t, "release_date": d} for t, d in pairs}
        return mock.patch.object(movies, "_search",
                                 side_effect=lambda t, _k: table.get(t))

    def test_skips_outside_the_daily_run(self):
        state: dict = {}
        with _env(TMDB_API_KEY="k"), \
                mock.patch.object(movies, "_search") as search:
            movies._weekly_countdown(state, today=MONDAY)
        search.assert_not_called()
        self.assertNotIn(movies.COUNTDOWN_KEY, state)

    def test_pushes_once_per_week_and_stamps(self):
        state: dict = {}
        with _env(NOTIFY_DAILY="1", TMDB_API_KEY="k"), \
                mock.patch.object(movies.watchlist, "titles",
                                  return_value=["Avengers: Doomsday"]), \
                self._films(("Avengers: Doomsday", "2026-06-26")):
            with capture_pushes() as sent:
                state = movies._weekly_countdown(state, today=MONDAY)
            self.assertEqual(len(sent), 1)
            self.assertIn("Avengers: Doomsday releases in 18 days",
                          sent[0]["message"])
            self.assertEqual(state[movies.COUNTDOWN_KEY], "2026-W24")
            # Second daily run in the same week: no second push.
            with capture_pushes() as sent:
                state = movies._weekly_countdown(state, today=MONDAY)
            self.assertEqual(sent, [])

    def test_quiet_week_stamps_without_pushing(self):
        state: dict = {}
        with _env(NOTIFY_DAILY="1", TMDB_API_KEY="k"), \
                mock.patch.object(movies.watchlist, "titles",
                                  return_value=["Far Future Film"]), \
                self._films(("Far Future Film", "2030-01-01")):
            with capture_pushes() as sent:
                state = movies._weekly_countdown(state, today=MONDAY)
        self.assertEqual(sent, [])
        self.assertEqual(state[movies.COUNTDOWN_KEY], "2026-W24")

    def test_missing_api_key_skips_without_stamping(self):
        state: dict = {}
        with _env(NOTIFY_DAILY="1"), \
                mock.patch.object(movies, "_search") as search:
            state = movies._weekly_countdown(state, today=MONDAY)
        search.assert_not_called()
        self.assertNotIn(movies.COUNTDOWN_KEY, state)

    def test_total_lookup_failure_retries_next_daily_run(self):
        state: dict = {}
        with _env(NOTIFY_DAILY="1", TMDB_API_KEY="k"), \
                mock.patch.object(movies.watchlist, "titles",
                                  return_value=["Some Film"]), \
                mock.patch.object(movies, "_search", side_effect=OSError("down")):
            with capture_pushes() as sent:
                state = movies._weekly_countdown(state, today=MONDAY)
        self.assertEqual(sent, [])
        self.assertNotIn(movies.COUNTDOWN_KEY, state)  # not stamped -> retried


if __name__ == "__main__":
    unittest.main()
