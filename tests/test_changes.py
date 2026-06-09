"""Tests for the reusable change-summary framework (notify_watcher.changes).

Pure cases per kind (number/date/string/set/list) pinning the rendered ``summary``
and the structured ``metadata`` move, the ``fmt``/``unit``/``template`` overrides, the
``previous == current -> None`` no-op, and the ``events.emit(change=...)`` keystone
(fills empty body, stashes metadata["change"], stays backward compatible when omitted).
"""
from __future__ import annotations

import datetime as _dt
import unittest

from notify_watcher import changes, events
from tests._util import capture_pushes

MINUS = changes._MINUS


class NoOpTest(unittest.TestCase):
    def test_equal_values_return_none(self):
        self.assertIsNone(changes.diff(5, 5))
        self.assertIsNone(changes.diff("a", "a"))
        self.assertIsNone(changes.diff([1, 2], [1, 2]))


class NumberTest(unittest.TestCase):
    def test_abs_and_pct_with_fmt(self):
        ch = changes.diff(58.2, 60.1, label="USD/DOP", fmt=lambda r: f"{r:.2f}")
        self.assertEqual(ch.summary, "USD/DOP moved from 58.20 to 60.10 (+1.90, +3.26%)")
        self.assertEqual(ch.kind, "number")
        self.assertEqual(ch.direction, "up")
        self.assertAlmostEqual(ch.metadata["abs_delta"], 1.9)
        self.assertAlmostEqual(ch.metadata["pct_delta"], 3.26, places=2)

    def test_downward_move_uses_minus(self):
        ch = changes.diff(100, 80, label="X")
        self.assertEqual(ch.direction, "down")
        self.assertEqual(ch.summary, f"X moved from 100 to 80 ({MINUS}20, {MINUS}20.00%)")

    def test_zero_previous_omits_pct(self):
        ch = changes.diff(0, 5, label="N")
        self.assertNotIn("%", ch.summary)
        self.assertNotIn("pct_delta", ch.metadata)
        self.assertEqual(ch.summary, "N moved from 0 to 5 (+5)")

    def test_numeric_strings_detected_as_number(self):
        ch = changes.diff("58.2", "60.1", label="R")
        self.assertEqual(ch.kind, "number")

    def test_unit_appended_to_delta(self):
        ch = changes.diff(10, 13, label="Load", unit=" req/s")
        self.assertIn("(+3 req/s", ch.summary)

    def test_currency_fmt_renders_values_and_delta(self):
        # Pins the deals topic's exact call: fmt applies to from/to AND the delta.
        def _fmt(p, cur="USD"):
            return f"{cur} {p:.2f}"
        ch = changes.diff(99.99, 79.99, label="Soundcore Liberty 4",
                          fmt=lambda p: _fmt(p))
        self.assertEqual(
            ch.summary,
            f"Soundcore Liberty 4 moved from USD 99.99 to USD 79.99 "
            f"({MINUS}USD 20.00, {MINUS}20.00%)")
        self.assertEqual(ch.direction, "down")

    def test_bool_is_not_a_number(self):
        ch = changes.diff(True, False)
        self.assertEqual(ch.kind, "string")


class DateTest(unittest.TestCase):
    def test_iso_date_forward_delta_with_fmt(self):
        ch = changes.diff("2027-05-26", "2027-09-18", kind="date", label="GTA VI",
                          fmt=lambda d: d.strftime("%b %d %Y").replace(" 0", " "))
        self.assertEqual(ch.summary, "GTA VI moved from May 26 2027 to Sep 18 2027 (+115 days)")
        self.assertEqual(ch.metadata["days"], 115)
        self.assertEqual(ch.direction, "up")

    def test_default_date_format(self):
        ch = changes.diff("2027-05-26", "2027-09-18", kind="date", label="GTA VI")
        self.assertEqual(ch.summary, "GTA VI moved from May 26 2027 to Sep 18 2027 (+115 days)")

    def test_visa_ddmonyy_format(self):
        ch = changes.diff("01JAN21", "15FEB21", kind="date", label="F4 Dates for Filing")
        self.assertEqual(ch.summary,
                         "F4 Dates for Filing moved from Jan 1 2021 to Feb 15 2021 (+45 days)")
        self.assertEqual(ch.metadata["days"], 45)

    def test_backward_move(self):
        ch = changes.diff("2027-09-18", "2027-05-26", kind="date", label="X")
        self.assertEqual(ch.direction, "down")
        self.assertIn(f"({MINUS}115 days)", ch.summary)

    def test_date_objects(self):
        ch = changes.diff(_dt.date(2027, 5, 26), _dt.date(2027, 9, 18), label="X")
        self.assertEqual(ch.kind, "date")
        self.assertEqual(ch.metadata["days"], 115)

    def test_unparseable_date_degrades_to_string(self):
        # A topic forcing kind="date" on a TBA transition still speaks (string diff).
        ch = changes.diff("2027-05-26", "TBA", kind="date", label="X")
        self.assertEqual(ch.kind, "string")
        self.assertEqual(ch.summary, 'X changed from "2027-05-26" to "TBA"')


class StringTest(unittest.TestCase):
    def test_string_summary(self):
        ch = changes.diff("draft", "final", label="Status")
        self.assertEqual(ch.summary, 'Status changed from "draft" to "final"')
        self.assertEqual(ch.direction, "changed")

    def test_no_label(self):
        ch = changes.diff("a", "b")
        self.assertEqual(ch.summary, 'changed from "a" to "b"')


class SetTest(unittest.TestCase):
    def test_added_and_removed(self):
        ch = changes.diff({"a", "b"}, {"b", "c"}, label="Tags")
        self.assertEqual(ch.kind, "set")
        self.assertEqual(ch.direction, "mixed")
        self.assertEqual(ch.metadata, {"added": ["c"], "removed": ["a"]})
        self.assertEqual(ch.summary, f"Tags +{{c}} / {MINUS}{{a}}")

    def test_only_added(self):
        ch = changes.diff({"a"}, {"a", "b"}, label="T")
        self.assertEqual(ch.direction, "added")
        self.assertEqual(ch.summary, "T +{b}")


class ListTest(unittest.TestCase):
    def test_order_aware_multiset(self):
        ch = changes.diff([1, 2, 2, 3], [2, 3, 4], label="L")
        self.assertEqual(ch.kind, "list")
        self.assertEqual(ch.metadata["added"], [4])
        self.assertEqual(ch.metadata["removed"], [1, 2])


class TemplateOverrideTest(unittest.TestCase):
    def test_template_replaces_summary(self):
        ch = changes.diff(58.2, 60.1, label="USD/DOP",
                          template=lambda c: f"rate up {c.metadata['abs_delta']:.1f}")
        self.assertEqual(ch.summary, "rate up 1.9")
        # structured metadata is preserved for the dashboard even when phrasing is custom
        self.assertAlmostEqual(ch.metadata["abs_delta"], 1.9)


# Engine config that pushes any event, so emit's transport path runs in tests
# (the live monitors.json engine would score/drop a synthetic fx event).
ENGINE_PUSH = {"threshold": 50, "digest_floor": 25, "default": 90, "rules": []}


class EmitIntegrationTest(unittest.TestCase):
    def test_change_fills_empty_body_and_stashes_metadata(self):
        ch = changes.diff(58.2, 60.1, label="USD/DOP", fmt=lambda r: f"{r:.2f}")
        with capture_pushes() as sent:
            state = events.emit({}, title="FX", topic="fx", source="FX", change=ch,
                                priority_cfg=ENGINE_PUSH)
        self.assertEqual(sent[0]["message"], "USD/DOP moved from 58.20 to 60.10 (+1.90, +3.26%)")
        rec = state["event_log"][0]
        self.assertEqual(rec["detail"], ch.summary)
        self.assertEqual(rec["topic"], "fx")

    def test_explicit_body_wins_over_change_summary(self):
        ch = changes.diff(58.2, 60.1, label="USD/DOP")
        with capture_pushes() as sent:
            events.emit({}, title="FX", topic="fx", source="FX",
                        body="custom", change=ch, priority_cfg=ENGINE_PUSH)
        self.assertEqual(sent[0]["message"], "custom")


if __name__ == "__main__":
    unittest.main()
