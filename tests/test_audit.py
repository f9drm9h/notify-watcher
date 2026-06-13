from __future__ import annotations

import datetime as _dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from notify_watcher import audit, control, digest, events
from tests._util import capture_pushes


UTC = _dt.timezone.utc


class AuditLogTest(unittest.TestCase):
    def test_record_keeps_last_five_per_topic(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "audit.json"
            for i in range(7):
                audit.record(
                    "movies",
                    f"Item {i}",
                    f"reason {i}",
                    score=i,
                    path=path,
                    now=_dt.datetime(2026, 6, 13, i, tzinfo=UTC),
                )
            audit.record("games", "Game item", "game reason", path=path)

            movies = audit.recent("movies", path=path)
            self.assertEqual([item["title"] for item in movies],
                             ["Item 2", "Item 3", "Item 4", "Item 5", "Item 6"])
            self.assertEqual(audit.recent("games", path=path)[0]["reason"],
                             "game reason")

    def test_score_drop_records_reason_in_audit_file(self):
        cfg = {
            "threshold": 60,
            "digest_floor": 25,
            "default": 30,
            "rules": [{"topic": "movies", "score": 15}],
        }
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(audit, "AUDIT_PATH", Path(td) / "audit.json"), \
                capture_pushes() as sent:
            state: dict = {}
            events.emit(
                state,
                title="Some sequel rumor",
                topic="movies",
                source="MovieBot",
                priority_cfg=cfg,
                digest_cfg={},
            )

            self.assertEqual(sent, [])
            self.assertNotIn(digest.BUFFER_KEY, state)
            items = audit.recent("movies")
            self.assertEqual(items[0]["title"], "Some sequel rumor")
            self.assertEqual(items[0]["source"], "MovieBot")
            self.assertEqual(items[0]["score"], 15)
            self.assertEqual(items[0]["reason"], "score 15 below digest floor 25")

    def test_muted_digest_drop_records_reason(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(audit, "AUDIT_PATH", Path(td) / "audit.json"), \
                capture_pushes() as sent:
            state = {"muted": {"movies": "2099-01-01T00:00:00+00:00"}}
            events.emit(
                state,
                title="Trailer chatter",
                topic="movies",
                source="MovieBot",
                legacy_action="digest",
                score=40,
                priority_cfg={},
                digest_cfg={},
            )

            self.assertEqual(sent, [])
            self.assertNotIn(digest.BUFFER_KEY, state)
            self.assertIn("topic muted until 2099-01-01T00:00:00+00:00",
                          audit.recent("movies")[0]["reason"])


class ExplainCommandTest(unittest.TestCase):
    def test_explain_command_reports_recent_drops_high_priority(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(audit, "AUDIT_PATH", Path(td) / "audit.json"):
            audit.record("movies", "Skipped trailer", "score 10 below digest floor 25",
                         source="MovieBot", score=10)
            with capture_pushes() as sent:
                control.dispatch(["explain movies"], {})

            self.assertEqual(sent[0]["title"], "Explain: Movies")
            self.assertEqual(sent[0]["priority"], "high")
            self.assertIn("Recently dropped items for Movies:", sent[0]["message"])
            self.assertIn("- MovieBot: Skipped trailer (score 10)",
                          sent[0]["message"])
            self.assertIn("Reason: score 10 below digest floor 25",
                          sent[0]["message"])

    def test_explain_command_reports_empty_topic(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(audit, "AUDIT_PATH", Path(td) / "audit.json"), \
                capture_pushes() as sent:
            control.dispatch(["explain movies"], {})

            self.assertEqual(sent[0]["priority"], "high")
            self.assertEqual(sent[0]["message"],
                             "No recently dropped items for Movies.")


if __name__ == "__main__":
    unittest.main()
