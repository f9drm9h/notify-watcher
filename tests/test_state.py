from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from notify_watcher import main, state as state_mod


class StateLoadTest(unittest.TestCase):
    def test_missing_state_starts_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            with mock.patch.object(state_mod, "STATE_PATH", path):
                self.assertEqual(state_mod.load(), {})

    def test_valid_state_loads_json(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            path.write_text(json.dumps({"seen": ["a"]}), encoding="utf-8")
            with mock.patch.object(state_mod, "STATE_PATH", path):
                self.assertEqual(state_mod.load(), {"seen": ["a"]})

    def test_corrupt_state_is_backed_up_and_raises(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            original = '{"seen": ['
            path.write_text(original, encoding="utf-8")

            with mock.patch.object(state_mod, "STATE_PATH", path):
                with self.assertRaises(state_mod.CorruptStateError) as ctx:
                    state_mod.load()

            self.assertIn("Refusing to continue", str(ctx.exception))
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            backups = list(Path(d).glob("state.json.corrupt-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), original)


class MainCorruptStateTest(unittest.TestCase):
    def test_load_failure_exits_before_save(self):
        with mock.patch.object(main.state_mod, "load",
                               side_effect=state_mod.CorruptStateError("bad")), \
                mock.patch.object(main.state_mod, "save") as save:
            with self.assertRaises(state_mod.CorruptStateError):
                main.main()

        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
