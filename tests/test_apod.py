"""Tests for the NASA APOD topic (notify_watcher.topics.apod)."""
from __future__ import annotations

import unittest
from unittest import mock

from notify_watcher.topics import apod
from tests._util import capture_pushes

IMAGE_DAY = {
    "date": "2026-06-09",
    "media_type": "image",
    "title": "The Sombrero Galaxy",
    "explanation": "A bright galaxy seen nearly edge-on. " * 20,
    "url": "https://apod.nasa.gov/image/sombrero_1024.jpg",
    "hdurl": "https://apod.nasa.gov/image/sombrero_4096.jpg",
    "copyright": "J. Doe",
}

VIDEO_DAY = {
    "date": "2026-06-10",
    "media_type": "video",
    "title": "Perseid Fireball",
    "explanation": "A meteor filmed over the desert.",
    "url": "https://www.youtube.com/watch?v=abc123",
    "thumbnail_url": "https://img.youtube.com/vi/abc123/0.jpg",
}


class ComposeTest(unittest.TestCase):
    def test_image_day_attaches_image_and_clicks_hd(self):
        title, body, attach, click = apod._compose(IMAGE_DAY)
        self.assertEqual(title, "APOD: The Sombrero Galaxy")
        self.assertEqual(attach, IMAGE_DAY["url"])
        self.assertEqual(click, IMAGE_DAY["hdurl"])
        self.assertLessEqual(len(body.splitlines()[0]), apod._MAX_CAPTION + 3)
        self.assertIn("(c) J. Doe", body)

    def test_image_without_hdurl_clicks_standard(self):
        data = {k: v for k, v in IMAGE_DAY.items() if k != "hdurl"}
        *_, click = apod._compose(data)
        self.assertEqual(click, IMAGE_DAY["url"])

    def test_video_day_attaches_thumbnail_and_clicks_video(self):
        title, _, attach, click = apod._compose(VIDEO_DAY)
        self.assertEqual(attach, VIDEO_DAY["thumbnail_url"])
        self.assertEqual(click, VIDEO_DAY["url"])

    def test_video_without_thumbnail_attaches_nothing(self):
        data = {k: v for k, v in VIDEO_DAY.items() if k != "thumbnail_url"}
        _, _, attach, click = apod._compose(data)
        self.assertIsNone(attach)
        self.assertEqual(click, VIDEO_DAY["url"])

    def test_unsendable_payloads_return_none(self):
        self.assertIsNone(apod._compose({}))
        self.assertIsNone(apod._compose({"title": "x", "media_type": "image"}))
        self.assertIsNone(apod._compose(dict(IMAGE_DAY, media_type="other")))


class RunTest(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict("os.environ", {"NOTIFY_DAILY": "1"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_sends_picture_with_attachment_once_per_date(self):
        with mock.patch.object(apod, "_fetch", return_value=IMAGE_DAY), \
             capture_pushes() as sent:
            state = apod.run({})
            state = apod.run(state)  # same APOD date -> dedup
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["title"], "APOD: The Sombrero Galaxy")
        self.assertEqual(sent[0]["attach_url"], IMAGE_DAY["url"])
        self.assertEqual(sent[0]["click_url"], IMAGE_DAY["hdurl"])
        self.assertEqual(state[apod.STATE_KEY], "2026-06-09")

    def test_new_date_sends_again(self):
        with mock.patch.object(apod, "_fetch", return_value=IMAGE_DAY), \
             capture_pushes() as sent:
            state = apod.run({})
        with mock.patch.object(apod, "_fetch", return_value=VIDEO_DAY), \
             capture_pushes() as sent2:
            apod.run(state)
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(sent2), 1)

    def test_fetch_failure_is_silent_and_retries_later(self):
        with mock.patch.object(apod, "_fetch", side_effect=RuntimeError("boom")), \
             capture_pushes() as sent:
            state = apod.run({})
        self.assertEqual(sent, [])
        self.assertNotIn(apod.STATE_KEY, state)  # unstamped -> retried next run

    def test_not_daily_run_is_a_noop(self):
        with mock.patch.dict("os.environ", {"NOTIFY_DAILY": ""}), \
             mock.patch.object(apod, "_fetch") as fetch, \
             capture_pushes() as sent:
            apod.run({})
        fetch.assert_not_called()
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
