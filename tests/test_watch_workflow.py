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
            "(steps.mode.outputs.run_mode == 'twitch' && 'twitch,habits' || '') }}",
            text,
        )

    def test_fast_lane_runs_habits_every_cycle(self):
        # The habit tracker's minute-level reminder slots (05:30, 21:00, 22:00
        # local) and its reaction-ack polling both need the 15-minute cadence;
        # a 3-hourly sweep would deliver them hours late.
        text = WATCH_YML.read_text(encoding="utf-8")

        self.assertIn("'twitch,habits'", text)

    def test_habits_channel_secret_is_passed_through(self):
        text = WATCH_YML.read_text(encoding="utf-8")

        self.assertIn("CHANNEL_HABITS: ${{ secrets.CHANNEL_HABITS }}", text)

    def test_github_schedule_trigger_stays_removed(self):
        # GitHub's schedule trigger delayed or dropped most 15-minute ticks
        # (116-min average gap vs the configured 15, Jul 6-12 2026), so the
        # cadence moved to the Cloudflare Worker cron, which dispatches this
        # workflow instead. Re-adding schedule: alongside workflow_dispatch
        # would double-fire the sweep whenever both land close together.
        text = WATCH_YML.read_text(encoding="utf-8")

        self.assertNotIn("\n  schedule:", text)
        self.assertNotIn("- cron:", text)
        self.assertIn("workflow_dispatch:", text)

    def test_worker_cadence_dispatch_keeps_scheduled_behavior(self):
        # The Worker passes scheduled=true. The mode decision must key on that
        # input (github.event_name is workflow_dispatch for every run now), and
        # NOTIFY_SCHEDULED must reach main.py so a twitch fast-lane run is not
        # mistaken for a /run topic dispatch (which would skip control work and
        # push "Run complete" feedback every 15 minutes).
        text = WATCH_YML.read_text(encoding="utf-8")

        self.assertIn("scheduled:", text)
        self.assertIn('if [ "${{ inputs.scheduled }}" != "true" ]', text)
        self.assertIn("NOTIFY_SCHEDULED: ${{ inputs.scheduled && '1' || '' }}", text)
        self.assertNotIn("github.event_name", text)


if __name__ == "__main__":
    unittest.main()
