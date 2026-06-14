"""Tests for the Discord delivery transport (notify_watcher.discord_delivery).

Covers the three jobs the module owns: topic->category->channel routing,
discord.Embed rendering (color/timestamp/image/tags), and the REST POST plus
its fail-loud config/HTTP contract.
"""
from __future__ import annotations

import unittest
from unittest import mock

import requests

from notify_watcher import discord_delivery as dd

# A full, valid environment for the happy-path send tests.
ENV = {
    "DISCORD_TOKEN": "tok",
    "CHANNEL_FINANCE": "111",
    "CHANNEL_DISCOVERY": "222",
    "CHANNEL_LOGS": "333",
    "CHANNEL_BRIEFING": "444",
    "CHANNEL_GENERAL": "999",
}


class RoutingTest(unittest.TestCase):
    def test_known_topics_map_to_categories(self):
        self.assertEqual(dd.category_for("fx"), "finance")
        self.assertEqual(dd.category_for("spending"), "finance")
        self.assertEqual(dd.category_for("twitch"), "discovery")
        self.assertEqual(dd.category_for("music"), "discovery")
        self.assertEqual(dd.category_for("soundcore_pro"), "discovery")
        self.assertEqual(dd.category_for("digest"), "briefing")
        self.assertEqual(dd.category_for("control"), "logs")

    def test_unmapped_topic_defaults_to_general(self):
        self.assertEqual(dd.category_for("weather"), "general")
        self.assertEqual(dd.category_for("brand_new_topic"), "general")
        self.assertEqual(dd.category_for(None), "general")
        self.assertEqual(dd.category_for(""), "general")

    def test_channel_for_resolves_from_env(self):
        with mock.patch.dict("os.environ", ENV, clear=True):
            self.assertEqual(dd.channel_for("fx"), "111")
            self.assertEqual(dd.channel_for("twitch"), "222")
            self.assertEqual(dd.channel_for("digest"), "444")
            self.assertEqual(dd.channel_for("control"), "333")

    def test_channel_for_falls_back_to_general(self):
        # finance/discovery unset -> their topics fall through to general.
        with mock.patch.dict("os.environ", {"CHANNEL_GENERAL": "999"}, clear=True):
            self.assertEqual(dd.channel_for("fx"), "999")
            self.assertEqual(dd.channel_for("weather"), "999")


class ColorTest(unittest.TestCase):
    def test_color_follows_category(self):
        self.assertEqual(dd.color_for("fx"), dd.CATEGORY_COLOR["finance"])
        self.assertEqual(dd.color_for("twitch"), dd.CATEGORY_COLOR["discovery"])
        self.assertEqual(dd.color_for("nope"), dd.CATEGORY_COLOR["general"])

    def test_critical_severity_overrides_to_red(self):
        self.assertEqual(dd.color_for("fx", "critical"),
                         dd.CATEGORY_COLOR["logs"])


class EmbedTest(unittest.TestCase):
    def test_embed_has_title_color_timestamp_description(self):
        d = dd.build_embed("fx", "USD up", "rate moved").to_dict()
        self.assertIn("USD up", d["title"])
        self.assertEqual(d["color"], dd.CATEGORY_COLOR["finance"])
        self.assertIn("timestamp", d)
        self.assertEqual(d["description"], "rate moved")

    def test_tag_maps_to_emoji_prefix(self):
        d = dd.build_embed("system", "done", "", tags="white_check_mark").to_dict()
        self.assertTrue(d["title"].startswith("✅"))

    def test_unknown_tag_is_ignored(self):
        d = dd.build_embed("fx", "plain", "", tags="not_a_real_tag").to_dict()
        self.assertEqual(d["title"], "plain")

    def test_attach_url_becomes_inline_image(self):
        d = dd.build_embed("apod", "pic", "", attach_url="https://x/y.jpg").to_dict()
        self.assertEqual(d["image"]["url"], "https://x/y.jpg")

    def test_long_title_truncated_to_limit(self):
        d = dd.build_embed("fx", "z" * 500, "").to_dict()
        self.assertLessEqual(len(d["title"]), 256)


class SendTest(unittest.TestCase):
    def _ok_response(self):
        resp = mock.Mock()
        resp.raise_for_status = mock.Mock()
        return resp

    def test_send_posts_embed_to_routed_channel(self):
        with mock.patch.dict("os.environ", ENV, clear=True), \
             mock.patch.object(dd.requests, "post",
                               return_value=self._ok_response()) as post:
            dd.send("fx", "USD up", "rate moved", severity="moderate")
        post.assert_called_once()
        self.assertIn("/channels/111/messages", post.call_args.args[0])
        body = post.call_args.kwargs["json"]
        self.assertEqual(len(body["embeds"]), 1)
        self.assertEqual(body["embeds"][0]["color"], dd.CATEGORY_COLOR["finance"])
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"],
                         "Bot tok")

    def test_unmapped_topic_posts_to_general_channel(self):
        with mock.patch.dict("os.environ", ENV, clear=True), \
             mock.patch.object(dd.requests, "post",
                               return_value=self._ok_response()) as post:
            dd.send("weather", "rainy", "bring an umbrella")
        self.assertIn("/channels/999/messages", post.call_args.args[0])

    def test_send_raises_without_token(self):
        env = {k: v for k, v in ENV.items() if k != "DISCORD_TOKEN"}
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertRaises(dd.DiscordConfigError):
                dd.send("fx", "t", "m")

    def test_send_raises_without_any_channel(self):
        with mock.patch.dict("os.environ", {"DISCORD_TOKEN": "tok"}, clear=True):
            with self.assertRaises(dd.DiscordConfigError):
                dd.send("fx", "t", "m")

    def test_send_propagates_http_error(self):
        resp = mock.Mock()
        resp.raise_for_status.side_effect = requests.HTTPError("429")
        with mock.patch.dict("os.environ", ENV, clear=True), \
             mock.patch.object(dd.requests, "post", return_value=resp):
            with self.assertRaises(requests.HTTPError):
                dd.send("fx", "t", "m")


if __name__ == "__main__":
    unittest.main()
