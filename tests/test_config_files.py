"""CI gate: the live config files must parse and satisfy their schemas.

Reliability layer, Phase 1 (docs/design/04-reliability-safety-layer.md). The
runtime loaders are deliberately fail-soft — a typo in watchlist.json,
monitors.json, reminders.json, or habits.json never crashes a scheduled run,
it just silently disables the feature while every topic still stamps a healthy
last_ok. These tests make CI fail-hard instead, so a bad hand-edit (these files
are edited directly on github.com) is caught before it ever reaches a runner.

Schema violations are reported all at once with their JSON paths rather than
stopping at the first, because the fix loop is a human editing JSON by hand.
Semantic rules JSON Schema cannot express (real calendar dates, uniqueness)
live in the same file.
"""
from __future__ import annotations

import datetime as dt
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = ROOT / "schemas"
CONFIG_NAMES = ("watchlist", "monitors", "reminders", "habits")


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class ConfigFilesTest(unittest.TestCase):
    def test_json_syntax(self):
        # json.JSONDecodeError carries line/column, the clearest possible
        # message for a stray comma or unclosed brace.
        for name in CONFIG_NAMES:
            with self.subTest(file=f"{name}.json"):
                _load(ROOT / f"{name}.json")

    def test_schemas_are_valid_schemas(self):
        for name in CONFIG_NAMES:
            with self.subTest(schema=f"{name}.schema.json"):
                Draft202012Validator.check_schema(_load(SCHEMAS / f"{name}.schema.json"))

    def test_configs_match_schemas(self):
        for name in CONFIG_NAMES:
            with self.subTest(file=f"{name}.json"):
                validator = Draft202012Validator(_load(SCHEMAS / f"{name}.schema.json"))
                errors = [
                    f"  {error.json_path}: {error.message}"
                    for error in sorted(
                        validator.iter_errors(_load(ROOT / f"{name}.json")),
                        key=lambda error: error.json_path,
                    )
                ]
                self.assertFalse(
                    errors,
                    f"{name}.json failed schema validation:\n" + "\n".join(errors),
                )

    def test_reminder_dates_are_real_dates(self):
        # The schema's YYYY-MM-DD pattern admits 2026-02-30; only date parsing
        # rejects it. reminders.py would silently skip such an entry forever.
        for reminder in _load(ROOT / "reminders.json").get("reminders", []):
            with self.subTest(reminder=reminder.get("id") or reminder.get("name")):
                dt.date.fromisoformat(reminder["date"])

    def test_ids_are_unique(self):
        # Duplicate ids break per-entry state keys and the Snooze reply button:
        # two entries would share dedup/snooze state and shadow each other.
        reminders_file = _load(ROOT / "reminders.json")
        habits = _load(ROOT / "habits.json").get("habits", [])
        for label, ids in (
            ("reminder id", [r["id"] for r in reminders_file.get("reminders", []) if "id" in r]),
            ("bill id", [b["id"] for b in reminders_file.get("bills", [])]),
            ("habit name", [h["name"] for h in habits]),
        ):
            with self.subTest(field=label):
                dupes = {i for i in ids if ids.count(i) > 1}
                self.assertFalse(dupes, f"duplicate {label}(s): {sorted(dupes)}")
