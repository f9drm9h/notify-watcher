"""Pins on .github/workflows/alert.yml — the monitoring layer's own contract.

alert.yml cannot be unit-tested by running it, but every bug the August 2026
reliability audit found there was a *textual* property of the file: the wrong
API query parameter, a missing conclusion in an `if:`, a lookup that could kill
its own job. Those are exactly the properties a file test can hold in place, and
each one below marks a regression that shipped silently for months.
"""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
ALERT_YML = ROOT / ".github" / "workflows" / "alert.yml"
WATCH_YML = ROOT / ".github" / "workflows" / "watch.yml"
WORKER_JS = ROOT / "worker" / "src" / "index.js"

FAILING_CONCLUSIONS = '["failure", "timed_out", "startup_failure"]'


class HeartbeatTest(unittest.TestCase):
    def test_heartbeat_asks_for_successful_runs_not_completed_ones(self):
        # THE bug: GitHub counts a FAILED run as completed, so status=completed
        # let a workflow that failed every 15 minutes refresh the heartbeat's
        # freshness stamp on every failure and look perfectly healthy.
        text = ALERT_YML.read_text(encoding="utf-8")

        self.assertIn("workflows/watch.yml/runs?status=success&per_page=1", text)
        self.assertNotIn("workflows/watch.yml/runs?status=completed", text)

    def test_heartbeat_api_failure_does_not_silence_the_alert(self):
        # bash -e would otherwise kill the step on a gh api error, taking the
        # notification with it. An unreadable answer must fall back to the epoch
        # so the age check trips and alerts.
        text = ALERT_YML.read_text(encoding="utf-8")

        self.assertIn('|| last=""', text)
        self.assertIn('${last:-1970-01-01T00:00:00Z}', text)

    def test_heartbeat_audits_the_alert_workflow_itself(self):
        # alert.yml failed 468 times in this repo's history and never said so,
        # because nothing watches the watcher. The scheduled job now does.
        text = ALERT_YML.read_text(encoding="utf-8")

        self.assertIn("workflows/alert.yml/runs?per_page=20", text)
        self.assertIn("alert workflow itself is failing", text)


class FailurePipelineTest(unittest.TestCase):
    def test_failure_job_covers_timeouts_and_startup_failures(self):
        # `conclusion == 'failure'` missed a job killed by the runner cap
        # (timed_out) and a workflow whose YAML never parsed (startup_failure).
        text = ALERT_YML.read_text(encoding="utf-8")

        self.assertIn(FAILING_CONCLUSIONS, text)
        self.assertNotIn("workflow_run.conclusion == 'failure'", text)

    def test_cancelled_runs_stay_excluded(self):
        # watch's concurrency group cancels superseded queued runs as routine
        # behavior; alerting on those would be pure noise. Checked against the
        # conclusion list itself, not the whole file — the surrounding comments
        # explain the exclusion and legitimately say the word.
        self.assertNotIn("cancelled", FAILING_CONCLUSIONS)
        text = ALERT_YML.read_text(encoding="utf-8")

        for line in text.splitlines():
            if "fromJSON(" in line:
                self.assertIn(FAILING_CONCLUSIONS, line)

    def test_streak_lookup_fails_open_and_cannot_skip_the_push(self):
        # The lookup exists only to SUPPRESS a duplicate alert, so it must never
        # be able to prevent one. A duplicate is a nuisance; a swallowed alert is
        # the bug this whole workflow exists to prevent.
        text = ALERT_YML.read_text(encoding="utf-8")

        self.assertIn('|| prev=""', text)
        self.assertIn('prev="unknown"', text)
        self.assertIn("if: always() && !contains(", text)

    def test_recovery_lookup_fails_closed_instead(self):
        # The mirror image, and the one place fail-open would be wrong: an
        # unusable answer must not invent a "recovered!" for something never
        # reported broken.
        text = ALERT_YML.read_text(encoding="utf-8")

        self.assertIn('|| prev="none"', text)

    def test_every_discord_push_retries_and_bounds_its_own_time(self):
        text = ALERT_YML.read_text(encoding="utf-8")

        self.assertEqual(text.count("curl -fsS --retry 3 --retry-all-errors --max-time 30"),
                         text.count("curl -fsS"))


class WatchTimeoutTest(unittest.TestCase):
    def test_watch_job_caps_its_own_runtime(self):
        # Without this the cap is GitHub's 6-hour default: one wedged HTTP call
        # holds the `watch` concurrency group for a third of a day while the run
        # shows as "in progress" — no failure, no alert, no notifications.
        text = WATCH_YML.read_text(encoding="utf-8")

        self.assertIn("timeout-minutes:", text)


class ExternalHeartbeatTest(unittest.TestCase):
    """The Cloudflare Worker check — the only monitor GitHub cannot take down."""

    def test_worker_checks_successful_runs_from_outside_github(self):
        # During the July 2026 billing lockout, all 468 alert runs were refused
        # before their first step: 449 watch failures, zero notifications. Only a
        # monitor hosted elsewhere can report that.
        text = WORKER_JS.read_text(encoding="utf-8")

        self.assertIn("externalHeartbeat", text)
        self.assertIn("/runs?status=success&per_page=1", text)

    def test_worker_heartbeat_runs_from_the_cron_handler(self):
        text = WORKER_JS.read_text(encoding="utf-8")

        self.assertIn("await externalHeartbeat(env)", text)

    def test_worker_heartbeat_never_breaks_the_cadence_dispatch(self):
        # The dispatch is what keeps the entire watcher running; a broken
        # heartbeat must not be able to take it down.
        text = WORKER_JS.read_text(encoding="utf-8")

        self.assertIn("external heartbeat error", text)

    def test_worker_heartbeat_no_ops_without_its_channel_secret(self):
        # Deploying before `wrangler secret put CHANNEL_LOGS` must degrade to
        # today's behavior, not error every 15 minutes.
        text = WORKER_JS.read_text(encoding="utf-8")

        self.assertIn("if (!env.CHANNEL_LOGS || !env.DISCORD_BOT_TOKEN)", text)


if __name__ == "__main__":
    unittest.main()
