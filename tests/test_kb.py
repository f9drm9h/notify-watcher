"""Tests for the shared knowledge-base engine (notify_watcher.kb)."""
from __future__ import annotations

import datetime as _dt
import json
import tempfile
import unittest
from pathlib import Path

from notify_watcher import kb


class KbTest(unittest.TestCase):
    def test_pick_is_deterministic_by_day(self):
        items = list("abcdefg")
        d1 = _dt.date(2026, 3, 14)
        self.assertEqual(kb.pick(items, day=d1), kb.pick(items, day=d1))  # stable
        # Index is day-of-year mod len.
        self.assertEqual(kb.pick(items, day=d1), items[kb.day_of_year(d1) % len(items)])

    def test_pick_offset_staggers(self):
        items = list("abcdefg")
        d = _dt.date(2026, 3, 14)
        self.assertEqual(
            kb.pick(items, offset=1, day=d),
            items[(kb.day_of_year(d) + 1) % len(items)],
        )

    def test_pick_empty_is_none(self):
        self.assertIsNone(kb.pick([]))

    def test_load_filters_and_requires_field(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "k.json"
            p.write_text(json.dumps([
                {"text": "ok"},
                {"text": ""},          # empty -> dropped
                {"src": "no text"},    # missing field -> dropped
                "not a dict",          # -> dropped
            ]), encoding="utf-8")
            out = kb.load(p)
            self.assertEqual(out, [{"text": "ok"}])

    def test_load_custom_field(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "k.json"
            p.write_text(json.dumps([{"tip": "hi"}, {"tip": ""}]), encoding="utf-8")
            self.assertEqual(kb.load(p, field="tip"), [{"tip": "hi"}])

    def test_load_missing_or_bad_file_is_empty(self):
        self.assertEqual(kb.load(Path("/no/such/file.json")), [])
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bad.json"
            p.write_text("{ not json", encoding="utf-8")
            self.assertEqual(kb.load(p), [])
            p.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
            self.assertEqual(kb.load(p), [])

    def test_shipped_kb_files_are_valid_and_nonempty(self):
        # Guards the curated data: every bundled channel must load with content.
        for name in ("science_facts.json", "tech_literacy.json",
                     "life_skills.json", "general_knowledge.json"):
            items = kb.load(kb.DATA_DIR / name)
            self.assertTrue(items, f"{name} is empty or invalid")
        self.assertTrue(kb.load(kb.DATA_DIR / "health_tips.json", field="tip"))


if __name__ == "__main__":
    unittest.main()
