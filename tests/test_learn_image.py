"""Unit tests for the Wikipedia picture-of-the-day extraction in learn.py.

All tests are pure (no network calls) and use only stdlib unittest.
"""
from __future__ import annotations

import unittest
from unittest import mock

from notify_watcher.topics import learn
from tests._util import capture_pushes

THUMB_URL = "https://upload.wikimedia.org/wikipedia/commons/thumb/sample/400px-sample.jpg"
FULL_URL = "https://upload.wikimedia.org/wikipedia/commons/sample/sample.jpg"


class WikiImageUrlTest(unittest.TestCase):
    def test_returns_thumbnail_source_when_present(self):
        feed = {
            "image": {
                "thumbnail": {"source": THUMB_URL, "width": 400, "height": 300},
                "image": {"source": FULL_URL, "width": 4000, "height": 3000},
            }
        }
        self.assertEqual(learn._wiki_image_url(feed), THUMB_URL)

    def test_falls_back_to_image_source_when_no_thumbnail(self):
        feed = {
            "image": {
                "image": {"source": FULL_URL, "width": 4000, "height": 3000},
            }
        }
        self.assertEqual(learn._wiki_image_url(feed), FULL_URL)

    def test_returns_none_when_image_key_missing(self):
        self.assertIsNone(learn._wiki_image_url({}))

    def test_returns_none_when_image_key_is_not_dict(self):
        self.assertIsNone(learn._wiki_image_url({"image": "not-a-dict"}))

    def test_returns_none_when_source_fields_missing(self):
        feed = {"image": {"thumbnail": {}, "image": {}}}
        self.assertIsNone(learn._wiki_image_url(feed))

    def test_returns_none_on_none_feed(self):
        # feed={} -> image key absent
        self.assertIsNone(learn._wiki_image_url({"image": None}))


class RunAttachesImageTest(unittest.TestCase):
    """Integration: run() passes attach_url when the feed includes an image."""

    FEED_WITH_IMAGE = {
        "tfa": {
            "normalizedtitle": "Test Article",
            "extract": "A short extract.",
            "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Test"}},
        },
        "onthisday": [{"text": "Something happened.", "year": 2000}],
        "image": {
            "thumbnail": {"source": THUMB_URL, "width": 400, "height": 300},
        },
    }

    def setUp(self):
        self._env = mock.patch.dict("os.environ", {"NOTIFY_DAILY": "1"})
        self._env.start()
        self.addCleanup(self._env.stop)
        # Neutralize the standalone knowledge push (covered by
        # tests/test_learn_knowledge.py) so only the daily push is asserted on.
        self._knowledge = mock.patch.object(
            learn, "_run_knowledge", side_effect=lambda state, now=None: state)
        self._knowledge.start()
        self.addCleanup(self._knowledge.stop)

    def test_run_passes_attach_url_from_feed_image(self):
        with mock.patch.object(learn, "_fetch_feed", return_value=self.FEED_WITH_IMAGE), \
             mock.patch.object(learn.summarize, "one_line", return_value=None), \
             capture_pushes() as sent:
            learn.run({})
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].get("attach_url"), THUMB_URL)

    def test_run_sends_without_attach_url_when_image_absent(self):
        feed_no_image = {k: v for k, v in self.FEED_WITH_IMAGE.items() if k != "image"}
        with mock.patch.object(learn, "_fetch_feed", return_value=feed_no_image), \
             mock.patch.object(learn.summarize, "one_line", return_value=None), \
             capture_pushes() as sent:
            learn.run({})
        self.assertEqual(len(sent), 1)
        # attach_url should be absent or None — a missing image never blocks the push
        self.assertIsNone(sent[0].get("attach_url"))


if __name__ == "__main__":
    unittest.main()
