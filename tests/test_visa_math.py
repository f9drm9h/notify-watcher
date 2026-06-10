"""Tests for the F4 wait estimator math (notify_watcher.visa_math).

Pure stdlib, no network: pins estimate_wait's pace math (calendar days advanced
divided by bulletin MONTHS elapsed, not by history entries), the
years_remaining ETA, the rendered pace sentence, and the history recorder's
same-month-correction / cap behavior.
"""
from __future__ import annotations

import unittest

from notify_watcher import visa_math


def _h(*pairs):
    return [{"cutoff": c, "bulletin": b} for c, b in pairs]


class EstimateWaitTest(unittest.TestCase):
    def test_empty_history_returns_none(self):
        self.assertIsNone(visa_math.estimate_wait([]))
        self.assertIsNone(visa_math.estimate_wait(None))

    def test_single_entry_returns_none(self):
        self.assertIsNone(visa_math.estimate_wait(_h(("08NOV08", "2026-06"))))

    def test_two_entries_known_pace(self):
        # 08NOV08 -> 08DEC08 is +30 days over one bulletin month.
        est = visa_math.estimate_wait(
            _h(("08NOV08", "2026-06"), ("08DEC08", "2026-07")))
        self.assertEqual(est["days_per_bulletin"], 30.0)
        self.assertEqual(est["bulletins"], 1)
        self.assertIsNone(est["years_remaining"])

    def test_unchanged_months_slow_the_pace(self):
        # +30 days, but 3 bulletin months elapsed (two with no entry because
        # the cutoff held still) -> 10 d/bulletin, not 30.
        est = visa_math.estimate_wait(
            _h(("08NOV08", "2026-04"), ("08DEC08", "2026-07")))
        self.assertEqual(est["days_per_bulletin"], 10.0)
        self.assertEqual(est["bulletins"], 3)

    def test_bulletin_months_span_year_rollover(self):
        est = visa_math.estimate_wait(
            _h(("08NOV08", "2026-11"), ("08DEC08", "2027-02")))
        self.assertEqual(est["bulletins"], 3)

    def test_years_remaining_with_known_priority_date(self):
        # Latest cutoff 01JAN10, priority date 2011-01-01: 365 days to cover
        # at 31 d/bulletin -> (365 / 31) bulletins ~= 11.77 months ~= 0.98 yr.
        est = visa_math.estimate_wait(
            _h(("01DEC09", "2026-06"), ("01JAN10", "2026-07")), "2011-01-01")
        self.assertEqual(est["days_per_bulletin"], 31.0)
        self.assertAlmostEqual(est["years_remaining"], 365 / 31 / 12, places=6)

    def test_priority_date_already_reached_is_zero(self):
        est = visa_math.estimate_wait(
            _h(("01DEC09", "2026-06"), ("01JAN10", "2026-07")), "2009-06-15")
        self.assertEqual(est["years_remaining"], 0.0)

    def test_retrogression_has_pace_but_no_eta(self):
        est = visa_math.estimate_wait(
            _h(("01JAN10", "2026-06"), ("01DEC09", "2026-07")), "2011-01-01")
        self.assertEqual(est["days_per_bulletin"], -31.0)
        self.assertIsNone(est["years_remaining"])

    def test_unparseable_priority_date_means_no_eta(self):
        est = visa_math.estimate_wait(
            _h(("01DEC09", "2026-06"), ("01JAN10", "2026-07")), "someday")
        self.assertIsNone(est["years_remaining"])

    def test_non_date_cutoffs_are_skipped(self):
        # "C" (current) / "U" (unavailable) cells don't parse as dates; with
        # fewer than two parseable entries left there is no estimate.
        self.assertIsNone(visa_math.estimate_wait(
            _h(("C", "2026-06"), ("08DEC08", "2026-07"))))
        # ...but they don't poison a longer history either.
        est = visa_math.estimate_wait(
            _h(("08NOV08", "2026-06"), ("U", "2026-07"), ("08DEC08", "2026-08")))
        self.assertEqual(est["days_per_bulletin"], 15.0)
        self.assertEqual(est["bulletins"], 2)


class PaceSentenceTest(unittest.TestCase):
    def test_none_renders_empty(self):
        self.assertEqual(visa_math.pace_sentence(None), "")

    def test_pace_only_without_priority_date(self):
        est = visa_math.estimate_wait(
            _h(("08NOV08", "2026-06"), ("08DEC08", "2026-07")))
        self.assertEqual(visa_math.pace_sentence(est),
                         "Advanced ~30 d/bulletin over 1 bulletin.")

    def test_with_priority_date_appends_eta(self):
        est = {"days_per_bulletin": 14.0, "bulletins": 6, "years_remaining": 4.2}
        self.assertEqual(
            visa_math.pace_sentence(est),
            "Advanced ~14 d/bulletin over 6 bulletins — ~4.2 yr to your priority date.")

    def test_current_priority_date(self):
        est = {"days_per_bulletin": 14.0, "bulletins": 6, "years_remaining": 0.0}
        self.assertIn("priority date is current", visa_math.pace_sentence(est))

    def test_retrogression_reads_retreated(self):
        est = {"days_per_bulletin": -7.0, "bulletins": 2, "years_remaining": None}
        self.assertEqual(visa_math.pace_sentence(est),
                         "Retreated ~7 d/bulletin over 2 bulletins.")


class RecordCutoffTest(unittest.TestCase):
    def test_appends_new_bulletin_month(self):
        history = visa_math.record_cutoff([], "08NOV08", "2026-06")
        history = visa_math.record_cutoff(history, "08DEC08", "2026-07")
        self.assertEqual(history, _h(("08NOV08", "2026-06"), ("08DEC08", "2026-07")))

    def test_same_month_correction_replaces_last_entry(self):
        history = visa_math.record_cutoff([], "08NOV08", "2026-06")
        history = visa_math.record_cutoff(history, "15NOV08", "2026-06")
        self.assertEqual(history, _h(("15NOV08", "2026-06")))

    def test_caps_at_history_max(self):
        history: list = []
        for i in range(visa_math.HISTORY_MAX + 6):
            history = visa_math.record_cutoff(
                history, "08NOV08", f"{2000 + i // 12}-{i % 12 + 1:02d}")
        self.assertEqual(len(history), visa_math.HISTORY_MAX)
        # Oldest entries are the ones evicted.
        self.assertEqual(history[-1]["bulletin"], "2002-06")


if __name__ == "__main__":
    unittest.main()
