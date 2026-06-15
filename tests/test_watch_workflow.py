from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WATCH_YML = ROOT / ".github" / "workflows" / "watch.yml"


class WatchWorkflowTest(unittest.TestCase):
    def test_only_input_does_not_force_daily_mode(self):
        text = WATCH_YML.read_text(encoding="utf-8")

        self.assertIn("NOTIFY_DAILY: ${{ inputs.daily && '1' || '' }}", text)
        self.assertNotIn("inputs.daily || inputs.only", text)

    def test_only_input_still_controls_notify_only(self):
        text = WATCH_YML.read_text(encoding="utf-8")

        self.assertIn(
            "NOTIFY_ONLY: ${{ inputs.only != '' && inputs.only || "
            "(steps.mode.outputs.run_mode == 'twitch' && 'twitch' || '') }}",
            text,
        )


if __name__ == "__main__":
    unittest.main()
