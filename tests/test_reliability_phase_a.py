"""Reliability Phase A regression tests.

Four bug-fix workstreams, each pinned here:

1. Delivery-failure retry ordering: monitor.run_source and twitch.run used to
   mark an item/streamer as seen/live BEFORE events.emit, so a raised delivery
   (Discord outage, rate-limit exhaustion) was swallowed per-item and the
   notification was permanently lost while the topic stayed green. Now the
   seen/live marking happens only after emit returns, so a failed push retries
   on the next run.

2. Health-contract adoption: topics that swallow their own fetch failures
   (log + return state) now report source_failed instead of letting main.py
   stamp a healthy last_ok for a dead source.

3. LLM story legs report health: learn's knowledge story and wikiquote's
   narration (see test_wikiquote) file source_failed when no LLM provider
   answers, instead of retrying silently forever.

4. deals auto-product eviction: an auto-discovered product whose URL 404s on
   AUTO_EVICT_404S consecutive checks is evicted from state["auto_products"];
   any non-404 outcome resets the streak, and watchlist products are immune.
"""
from __future__ import annotations

import datetime as _dt
import unittest
from unittest import mock

import requests

from notify_watcher import health, ids, monitor
from notify_watcher.topics import (
    air_quality,
    anthropic_news,
    deals,
    energy,
    energy_learn,
    fda,
    golden_sun,
    habits,
    holidays,
    iss,
    itsc,
    launches,
    learn,
    marine,
    music,
    soundcore_pro,
    spending,
    twitch,
    uv,
)
from tests._util import capture_pushes

SCORING = {
    "source_weights": {"regulatory": 5, "trade": 1},
    "signal_bonuses": {"action": {"weight": 3, "terms": ["approves"]}},
    "noise_penalties": {},
    "thresholds": {"breakthrough": 8, "high": 6, "moderate": 4},
}
DIGEST = {"max_buffer": 50, "max_items_in_message": 25}


def _item(iid, title, weight="trade"):
    return {"id": iid, "title": title, "url": f"http://x/{iid}",
            "source": "Src", "weight": weight}


# ---------------------------------------------------------------------------
# 1. monitor.run_source: emit failure leaves the item unseen (retried)
# ---------------------------------------------------------------------------
class MonitorEmitFailureTest(unittest.TestCase):
    def setUp(self):
        # Engine OFF so run_source exercises its legacy tier routing.
        p = mock.patch("notify_watcher.config.section", return_value={})
        p.start()
        self.addCleanup(p.stop)

    def _run(self, state, items):
        return monitor.run_source(
            state, state_key="k", items=items, default_weight_key="trade",
            keywords=[], scoring_cfg=SCORING, digest_cfg=DIGEST, cap=100,
            live_title_prefix="Test",
        )

    def test_failed_emit_leaves_item_unseen_for_retry(self):
        state = {"k": []}
        items = [_item("hi", "approves drug", "regulatory")]  # -> live push
        with mock.patch.object(monitor.events, "emit",
                               side_effect=RuntimeError("discord down")):
            state = self._run(state, items)
        # The delivery failed, so the item must NOT be recorded as seen …
        self.assertEqual(state["k"], [])
        # … and the next (healthy) run must deliver it.
        with capture_pushes() as sent:
            state = self._run(state, items)
        self.assertEqual(len(sent), 1)
        self.assertEqual(state["k"], [ids.short("hi")])

    def test_one_failed_item_does_not_block_or_unmark_the_rest(self):
        state = {"k": []}
        items = [
            _item("bad", "approves drug", "regulatory"),   # emit raises
            _item("good", "approves cure", "regulatory"),  # emit succeeds
        ]
        calls = {"n": 0}

        def flaky_emit(state, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("discord down")
            return state

        with mock.patch.object(monitor.events, "emit", side_effect=flaky_emit):
            state = self._run(state, items)
        self.assertEqual(state["k"], [ids.short("good")])

    def test_dropped_minor_items_are_still_marked_seen(self):
        # A minor item never reaches emit, so it must keep the old "recorded
        # regardless" semantics and never be re-scored.
        state = {"k": []}
        with capture_pushes():
            state = self._run(state, [_item("low", "routine note", "trade")])
        self.assertEqual(state["k"], [ids.short("low")])


# ---------------------------------------------------------------------------
# 1b. twitch.run: emit failure leaves the session un-recorded (retried)
# ---------------------------------------------------------------------------
class TwitchEmitFailureTest(unittest.TestCase):
    def setUp(self):
        # Engine OFF for non-twitch sections, mirroring test_twitch.py.
        p = mock.patch.object(twitch.config, "section",
                              side_effect=lambda n: {"streamers": ["Sparg0"]}
                              if n == "twitch" else {})
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def _fake_get(kind, user):
        return {"uptime": "3h 2m 1s", "title": "GF", "game": "Melee"}[kind]

    def test_failed_live_push_is_retried_next_run(self):
        state: dict = {}
        with mock.patch.object(twitch, "_get", side_effect=self._fake_get), \
                mock.patch.object(twitch.events, "emit",
                                  side_effect=RuntimeError("discord down")):
            state = twitch.run(state)
        # The push never left the runner, so the session must NOT be recorded …
        self.assertEqual(state[twitch.STATE_KEY], [])
        # … and the next (healthy) run must re-alert it.
        with mock.patch.object(twitch, "_get", side_effect=self._fake_get), \
                capture_pushes() as sent:
            state = twitch.run(state)
        self.assertEqual(len(sent), 1)
        self.assertEqual(state[twitch.STATE_KEY], ["Sparg0"])

    def test_already_alerted_session_is_preserved(self):
        # A streamer alerted on a prior run stays recorded without re-alerting.
        state = {twitch.STATE_KEY: ["Sparg0"]}
        with mock.patch.object(twitch, "_get", side_effect=self._fake_get), \
                capture_pushes() as sent:
            state = twitch.run(state)
        self.assertEqual(sent, [])
        self.assertEqual(state[twitch.STATE_KEY], ["Sparg0"])


# ---------------------------------------------------------------------------
# 2. Health-contract adoption
# ---------------------------------------------------------------------------
NEWLY_ADOPTED = [
    "air_quality", "anthropic_news", "energy", "energy_learn",
    "fda", "golden_sun", "habits", "holidays", "iss", "itsc", "launches",
    "marine", "music", "soundcore_pro", "spending", "uv",
]


class AdoptionMembershipTest(unittest.TestCase):
    def test_new_topics_are_on_the_contract(self):
        for name in NEWLY_ADOPTED:
            with self.subTest(topic=name):
                self.assertIn(name, health.ADOPTED)


class FetchFailureClaimsTest(unittest.TestCase):
    """Each newly adopted topic files source_failed where it used to swallow."""

    def _status(self, state, topic):
        return health.consume(state, topic)

    def test_air_quality(self):
        with mock.patch.object(air_quality.requests, "get",
                               side_effect=RuntimeError("down")), \
                capture_pushes():
            state = air_quality.run({})
        self.assertTrue(self._status(state, "air_quality")["source_failed"])

    def test_air_quality_ok_claim(self):
        resp = mock.Mock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {"current": {"us_aqi": 40, "pm2_5": 5}}
        with mock.patch.object(air_quality.requests, "get", return_value=resp), \
                capture_pushes():
            state = air_quality.run({})
        self.assertTrue(self._status(state, "air_quality")["ok"])

    def test_anthropic_news(self):
        with mock.patch.object(anthropic_news.requests, "get",
                               side_effect=RuntimeError("down")), \
                capture_pushes():
            state = anthropic_news.run({})
        self.assertTrue(self._status(state, "anthropic_news")["source_failed"])

    def test_holidays_fetch_failure_and_parse_to_zero(self):
        with mock.patch.dict("os.environ", {"NOTIFY_DAILY": "1"}):
            with mock.patch.object(holidays, "_fetch",
                                   side_effect=RuntimeError("down")), \
                    capture_pushes():
                state = holidays.run({})
            self.assertTrue(self._status(state, "holidays")["source_failed"])
            with mock.patch.object(holidays, "_fetch", return_value=[]), \
                    capture_pushes():
                state = holidays.run({})
            self.assertTrue(self._status(state, "holidays")["source_failed"])

    def test_iss(self):
        with mock.patch.object(iss.requests, "get",
                               side_effect=RuntimeError("down")), \
                capture_pushes():
            state = iss.run({})
        self.assertTrue(self._status(state, "iss")["source_failed"])

    def test_uv(self):
        with mock.patch.dict("os.environ", {"NOTIFY_DAILY": "1"}), \
                mock.patch.object(uv.requests, "get",
                                  side_effect=RuntimeError("down")), \
                capture_pushes():
            state = uv.run({})
        self.assertTrue(self._status(state, "uv")["source_failed"])

    def test_launches_fetch_failure_and_empty_results(self):
        with mock.patch.object(launches.requests, "get",
                               side_effect=RuntimeError("down")), \
                capture_pushes():
            state = launches.run({})
        self.assertTrue(self._status(state, "launches")["source_failed"])
        resp = mock.Mock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {"results": []}
        with mock.patch.object(launches.requests, "get", return_value=resp), \
                capture_pushes():
            state = launches.run({})
        self.assertTrue(self._status(state, "launches")["source_failed"])

    def test_fda(self):
        with mock.patch.object(fda.requests, "get",
                               side_effect=RuntimeError("down")), \
                capture_pushes():
            state = fda.run({})
        self.assertTrue(self._status(state, "fda")["source_failed"])

    def test_energy_all_sources_down_vs_one_alive(self):
        cfg = {"sources": [{"name": "A", "url": "http://a"},
                           {"name": "B", "url": "http://b"}]}
        sections = lambda name: cfg if name == "energy" else {}
        with mock.patch.object(energy.config, "section", side_effect=sections), \
                mock.patch.object(energy, "_fetch",
                                  side_effect=RuntimeError("down")), \
                capture_pushes():
            state = energy.run({})
        self.assertTrue(self._status(state, "energy")["source_failed"])
        with mock.patch.object(energy.config, "section", side_effect=sections), \
                mock.patch.object(energy, "_fetch",
                                  side_effect=[RuntimeError("down"), []]), \
                capture_pushes():
            state = energy.run({})
        self.assertTrue(self._status(state, "energy")["ok"])

    def test_golden_sun_all_sources_down_vs_one_alive(self):
        with mock.patch.object(golden_sun, "_collect_wiki",
                               side_effect=RuntimeError("down")), \
                mock.patch.object(golden_sun, "_collect_reddit",
                                  side_effect=RuntimeError("down")), \
                mock.patch.object(golden_sun, "_collect_google_news",
                                  side_effect=RuntimeError("down")), \
                capture_pushes():
            state = golden_sun.run({})
        self.assertTrue(self._status(state, "golden_sun")["source_failed"])
        with mock.patch.object(golden_sun, "_collect_wiki", return_value=[]), \
                mock.patch.object(golden_sun, "_collect_reddit",
                                  side_effect=RuntimeError("down")), \
                mock.patch.object(golden_sun, "_collect_google_news",
                                  side_effect=RuntimeError("down")), \
                capture_pushes():
            state = golden_sun.run({})
        self.assertTrue(self._status(state, "golden_sun")["ok"])

    def test_itsc_page_unreachable_and_zero_rows(self):
        with mock.patch.dict("os.environ", {"NOTIFY_DAILY": "1"}):
            with mock.patch.object(itsc, "_collect_rows", return_value=None), \
                    capture_pushes():
                state = itsc.run({})
            self.assertTrue(self._status(state, "itsc")["source_failed"])
            with mock.patch.object(itsc, "_collect_rows",
                                   return_value=([], "http://itsc")), \
                    capture_pushes():
                state = itsc.run({})
            self.assertTrue(self._status(state, "itsc")["source_failed"])

    def test_music_all_lookups_down_vs_api_answering(self):
        sections = lambda n: ({"followed_artists": ["A"]} if n == "music" else {})
        with mock.patch.object(music.config, "section", side_effect=sections), \
                mock.patch.object(music, "_get",
                                  side_effect=RuntimeError("down")), \
                capture_pushes():
            state = music.run({})
        self.assertTrue(self._status(state, "music")["source_failed"])
        with mock.patch.object(music.config, "section", side_effect=sections), \
                mock.patch.object(music, "_get",
                                  return_value={"data": []}), \
                capture_pushes():
            state = music.run({})
        self.assertTrue(self._status(state, "music")["ok"])

    def test_habits_all_failed_vs_none_configured(self):
        habit = {"name": "water", "hours": [12], "messages": ["drink"]}
        with mock.patch.object(habits, "_load", return_value=[habit]), \
                mock.patch.object(habits, "_run_one",
                                  side_effect=RuntimeError("boom")), \
                capture_pushes():
            state = habits.run({})
        self.assertTrue(self._status(state, "habits")["source_failed"])
        with mock.patch.object(habits, "_load", return_value=[]), \
                capture_pushes():
            state = habits.run({})
        self.assertIsNone(self._status(state, "habits"))

    def test_spending_failure_vs_unconfigured(self):
        creds = {"GMAIL_USER": "u@x.com", "GMAIL_APP_PASSWORD": "pw",
                 "NOTIFY_DAILY": ""}
        with mock.patch.dict("os.environ", creds), \
                mock.patch.object(spending, "_ingest_emails",
                                  side_effect=RuntimeError("imap down")), \
                capture_pushes():
            state = spending.run({})
        self.assertTrue(self._status(state, "spending")["source_failed"])
        with mock.patch.dict("os.environ", {"GMAIL_USER": "",
                                            "GMAIL_APP_PASSWORD": "",
                                            "NOTIFY_DAILY": ""}), \
                mock.patch.object(spending, "_ingest_emails", return_value=0), \
                capture_pushes():
            state = spending.run({})
        self.assertIsNone(self._status(state, "spending"))

    def test_spending_ok_claim_when_configured(self):
        creds = {"GMAIL_USER": "u@x.com", "GMAIL_APP_PASSWORD": "pw",
                 "NOTIFY_DAILY": ""}
        with mock.patch.dict("os.environ", creds), \
                mock.patch.object(spending, "_ingest_emails", return_value=(2, 5)), \
                capture_pushes():
            state = spending.run({})
        status = self._status(state, "spending")
        self.assertTrue(status["ok"])
        # data_count is bank EMAILS SEEN (5), not new transactions (2): a quiet
        # week adds nothing but still proves the mailbox pipe is alive.
        self.assertEqual(status["data_count"], 5)

    def test_spending_alive_mailbox_with_no_new_rows_is_not_empty(self):
        # The distinction the Aug 2026 audit added: emails arriving but all
        # already recorded must NOT start the watchdog's quiet clock.
        creds = {"GMAIL_USER": "u@x.com", "GMAIL_APP_PASSWORD": "pw",
                 "NOTIFY_DAILY": ""}
        with mock.patch.dict("os.environ", creds), \
                mock.patch.object(spending, "_ingest_emails", return_value=(0, 4)), \
                capture_pushes():
            state = spending.run({})
        self.assertEqual(self._status(state, "spending")["data_count"], 4)

    def test_energy_learn_no_content_vs_sent(self):
        entry = {"id": "x", "title": "Volt", "what": "w", "why": "y", "care": "c"}
        with mock.patch.dict("os.environ", {"NOTIFY_DAILY": "1"}):
            with mock.patch.object(energy_learn, "_curated_channel",
                                   return_value=("", "", [])), \
                    capture_pushes():
                state = energy_learn.run({})
            self.assertTrue(
                self._status(state, "energy_learn")["source_failed"])
            with mock.patch.object(energy_learn, "_curated_channel",
                                   return_value=("history", "Historical event",
                                                 [entry])), \
                    capture_pushes():
                state = energy_learn.run({})
            self.assertTrue(self._status(state, "energy_learn")["ok"])


# ---------------------------------------------------------------------------
# 3. learn's knowledge story leg reports LLM failure
# ---------------------------------------------------------------------------
class LearnKnowledgeHealthTest(unittest.TestCase):
    # The knowledge story is spark's "fact" leg now, so its claims land under
    # the spark topic (learn.SPARK_TOPIC) rather than "learn".
    def test_generation_failure_reports_source_failed(self):
        chosen = {"id": "t1", "title": "The Aztec calendar", "category": "history"}
        state: dict = {}
        with mock.patch.object(learn, "_knowledge_pick",
                               return_value=chosen), \
                mock.patch.object(learn.summarize, "brief", return_value=None), \
                capture_pushes() as sent:
            sent_ok = learn.send_knowledge(state)
        self.assertEqual(sent, [])
        status = health.consume(state, learn.SPARK_TOPIC)
        self.assertTrue(status["source_failed"])
        self.assertIn("knowledge story generation failed", status["message"])
        # send_knowledge reports failure so spark leaves its window unstamped
        # and the next run retries.
        self.assertFalse(sent_ok)

    def test_empty_kb_reports_source_failed(self):
        state: dict = {}
        with mock.patch.object(learn, "_knowledge_pick", return_value=None), \
                capture_pushes():
            self.assertFalse(learn.send_knowledge(state))
        self.assertTrue(health.consume(state, learn.SPARK_TOPIC)["source_failed"])


# ---------------------------------------------------------------------------
# 4. deals: consecutive-404 eviction of auto-discovered products
# ---------------------------------------------------------------------------
JUNK_URL = "https://www.soundcore.com/products/mis-parsed-bundle"


class _Resp404:
    status_code = 404

    def raise_for_status(self):
        raise requests.HTTPError("404 Client Error", response=self)


class DealsAutoEvictionTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(deals.watchlist, "entries", return_value=[])
        p.start()
        self.addCleanup(p.stop)

    def _state(self):
        return {"auto_products": [{"name": "Junk", "url": JUNK_URL}]}

    def _run_404(self, state):
        with mock.patch.object(deals.requests, "get", return_value=_Resp404()), \
                capture_pushes():
            return deals.run(state)

    def test_evicted_after_three_consecutive_404s(self):
        state = self._state()
        state = self._run_404(state)
        self.assertEqual(state[deals.AUTO_404_KEY][JUNK_URL], 1)
        self.assertEqual(len(state["auto_products"]), 1)
        state = self._run_404(state)
        self.assertEqual(state[deals.AUTO_404_KEY][JUNK_URL], 2)
        state = self._run_404(state)
        self.assertEqual(state["auto_products"], [])
        self.assertNotIn(deals.AUTO_404_KEY, state)  # counter cleaned up too

    def test_non_404_failure_resets_the_streak(self):
        state = self._state()
        state = self._run_404(state)
        state = self._run_404(state)
        self.assertEqual(state[deals.AUTO_404_KEY][JUNK_URL], 2)
        with mock.patch.object(deals.requests, "get",
                               side_effect=RuntimeError("timeout")), \
                capture_pushes():
            state = deals.run(state)
        self.assertNotIn(deals.AUTO_404_KEY, state)
        self.assertEqual(len(state["auto_products"]), 1)  # still tracked

    def test_watchlist_products_are_never_evicted(self):
        wl = [{"name": "Config product", "url": "https://shop/x"}]
        with mock.patch.object(deals.watchlist, "entries", return_value=wl):
            state: dict = {}
            for _ in range(deals.AUTO_EVICT_404S + 1):
                with mock.patch.object(deals.requests, "get",
                                       return_value=_Resp404()), \
                        capture_pushes():
                    state = deals.run(state)
        # No counter is ever kept for a watchlist product, nothing evicted.
        self.assertNotIn(deals.AUTO_404_KEY, state)


if __name__ == "__main__":
    unittest.main()
