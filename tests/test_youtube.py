"""Tests for the YouTube uploads watcher (notify_watcher.topics.youtube)."""
from __future__ import annotations

import unittest
from unittest import mock

from notify_watcher.topics import youtube
from tests._util import capture_pushes

CFG = {"channels": [{"channel_id": "UC_ONE", "name": "Channel One"}]}

FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns="http://www.w3.org/2005/Atom">
  <title>Channel One uploads</title>
  <entry>
    <id>yt:video:vid_new_111</id>
    <yt:videoId>vid_new_111</yt:videoId>
    <title>Newest video</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=vid_new_111"/>
  </entry>
  <entry>
    <id>yt:video:vid_old_222</id>
    <yt:videoId>vid_old_222</yt:videoId>
    <title>Older video</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=vid_old_222"/>
  </entry>
</feed>"""


class ParseFeedTest(unittest.TestCase):
    def test_extracts_ids_and_titles_in_feed_order(self):
        videos = youtube.parse_feed(FEED_XML)
        self.assertEqual(videos, [("vid_new_111", "Newest video"),
                                  ("vid_old_222", "Older video")])

    def test_entry_without_video_id_is_skipped(self):
        xml = """<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
                       xmlns="http://www.w3.org/2005/Atom">
                   <entry><title>No id here</title></entry>
                 </feed>"""
        self.assertEqual(youtube.parse_feed(xml), [])

    def test_empty_feed_yields_no_videos(self):
        xml = '<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
        self.assertEqual(youtube.parse_feed(xml), [])


def _response(text: str) -> mock.Mock:
    resp = mock.Mock()
    resp.text = text
    resp.raise_for_status.return_value = None
    return resp


def _run(state, cfg=CFG, get=None):
    """Run the topic with config + HTTP mocked, capturing would-be pushes."""
    if get is None:
        get = mock.Mock(return_value=_response(FEED_XML))
    with mock.patch.object(youtube.requests, "get", get), \
         mock.patch.object(youtube.config, "section",
                           side_effect=lambda n: cfg if n == "youtube" else {}), \
         capture_pushes() as sent:
        state = youtube.run(state)
    return state, sent


class RunTest(unittest.TestCase):
    def test_first_run_seeds_silently(self):
        state, sent = _run({})
        self.assertEqual(sent, [])
        self.assertEqual(sorted(state[youtube.STATE_KEY]["UC_ONE"]),
                         ["vid_new_111", "vid_old_222"])

    def test_new_video_pushes_with_click_url(self):
        state, sent = _run({youtube.STATE_KEY: {"UC_ONE": ["vid_old_222"]}})
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["title"], "Channel One uploaded a new video")
        self.assertEqual(sent[0]["message"], "Newest video")
        self.assertEqual(sent[0]["click_url"],
                         "https://www.youtube.com/watch?v=vid_new_111")
        self.assertEqual(sent[0]["tags"], "youtube,tv")
        self.assertIn("vid_new_111", state[youtube.STATE_KEY]["UC_ONE"])

    def test_already_seen_videos_do_not_repush(self):
        state, sent = _run(
            {youtube.STATE_KEY: {"UC_ONE": ["vid_new_111", "vid_old_222"]}})
        self.assertEqual(sent, [])
        # Dedup memory is preserved, not lost, on a quiet run.
        self.assertEqual(sorted(state[youtube.STATE_KEY]["UC_ONE"]),
                         ["vid_new_111", "vid_old_222"])

    def test_no_channels_configured_is_a_noop(self):
        state, sent = _run({}, cfg={})
        self.assertEqual(sent, [])
        self.assertNotIn(youtube.STATE_KEY, state)

    def test_channel_added_later_seeds_silently(self):
        # UC_ONE is established; UC_TWO is new to the config. Only UC_ONE's
        # genuinely new video pushes — UC_TWO's backlog is seeded, not blasted.
        cfg = {"channels": [{"channel_id": "UC_ONE", "name": "Channel One"},
                            {"channel_id": "UC_TWO", "name": "Channel Two"}]}
        state, sent = _run({youtube.STATE_KEY: {"UC_ONE": ["vid_old_222"]}},
                           cfg=cfg)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["title"], "Channel One uploaded a new video")
        self.assertEqual(sorted(state[youtube.STATE_KEY]["UC_TWO"]),
                         ["vid_new_111", "vid_old_222"])

    def test_legacy_flat_list_state_migrates_without_pushing(self):
        # Pre-migration state was one flat list across all channels. It is
        # replaced by the per-channel map via a silent re-seed: no pushes,
        # and everything currently in the feed is remembered.
        state, sent = _run({youtube.STATE_KEY: ["vid_old_222"]})
        self.assertEqual(sent, [])
        self.assertEqual(sorted(state[youtube.STATE_KEY]["UC_ONE"]),
                         ["vid_new_111", "vid_old_222"])

    def test_one_bad_channel_does_not_block_the_others(self):
        cfg = {"channels": [{"channel_id": "UC_BAD", "name": "Broken"},
                            {"channel_id": "UC_ONE", "name": "Channel One"}]}

        def get(url, **kwargs):
            if "UC_BAD" in url:
                raise RuntimeError("connection refused")
            return _response(FEED_XML)

        state, sent = _run({youtube.STATE_KEY: {"UC_BAD": ["vid_b_1"],
                                                "UC_ONE": ["vid_old_222"]}},
                           cfg=cfg, get=get)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["message"], "Newest video")
        # The broken channel's memory survives the outage.
        self.assertEqual(state[youtube.STATE_KEY]["UC_BAD"], ["vid_b_1"])

    def test_unreachable_first_sight_channel_stays_unseeded(self):
        # A channel whose feed fails before it was ever seeded must stay
        # absent from state, so it still seeds silently (no backlog blast)
        # once its feed becomes reachable.
        cfg = {"channels": [{"channel_id": "UC_BAD", "name": "Broken"},
                            {"channel_id": "UC_ONE", "name": "Channel One"}]}
        get = mock.Mock(side_effect=RuntimeError("connection refused"))
        state, sent = _run({youtube.STATE_KEY: {"UC_ONE": ["vid_old_222"]}},
                           cfg=cfg, get=get)
        self.assertEqual(sent, [])
        self.assertNotIn("UC_BAD", state[youtube.STATE_KEY])
        # The reachable-yesterday channel keeps its memory through the outage.
        self.assertEqual(state[youtube.STATE_KEY]["UC_ONE"], ["vid_old_222"])


if __name__ == "__main__":
    unittest.main()
