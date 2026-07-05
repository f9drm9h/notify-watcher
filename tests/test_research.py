"""Tests for the on-demand article summary (notify_watcher.topics.research).

run() is driven entirely by NOTIFY_RESEARCH_URL: empty means scheduled-run
no-op; set means fetch -> extract -> summarize -> post. Every failure path
must still post a short reply (the command is user-initiated, so silence
reads as a hang) and run() must never raise.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

import requests

from notify_watcher.topics import research
from tests._util import capture_pushes

URL = "https://example.com/story"

# A readable article: enough prose to clear MIN_WORDS, wrapped in the
# boilerplate elements _extract must strip.
_BODY = " ".join(f"fact{i}" for i in range(research.MIN_WORDS + 40))
ARTICLE_HTML = f"""
<html><head><title>Chip Fab Expansion</title><script>track()</script></head>
<body>
  <nav>Home | Subscribe | Sign in</nav>
  <article><h1>Chip Fab Expansion</h1><p>{_BODY}</p></article>
  <footer>(c) Example News</footer>
</body></html>
"""

# A paywall shell: a title and a teaser, nowhere near MIN_WORDS of prose.
SHORT_HTML = """
<html><head><title>Members Only</title></head>
<body><article><p>Subscribe to keep reading this story.</p></article></body></html>
"""


class _Resp:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self) -> None:
        pass


def _env(url: str):
    return mock.patch.dict(os.environ, {research.ENV_KEY: url})


class ResearchRunTest(unittest.TestCase):
    def test_non_url_input_is_rejected_without_fetch_or_click_url(self):
        # Discord returns 400 for an embed whose url field is not a valid
        # http(s) URL, so junk input must be rejected before any fetch and the
        # rejection reply must not carry a click_url at all.
        for bad in ("not a url", "example.com/story", "ftp://example.com/x",
                    "javascript:alert(1)", "http://"):
            with self.subTest(bad=bad), _env(bad), \
                    mock.patch.object(research.requests, "get") as get, \
                    mock.patch.object(research.summarize, "brief") as brief, \
                    capture_pushes() as sent:
                research.run({})  # must not raise
            get.assert_not_called()
            brief.assert_not_called()
            self.assertEqual(len(sent), 1)
            self.assertIn("doesn't look like a URL", sent[0]["message"])
            self.assertIsNone(sent[0].get("click_url"))

    def test_valid_url_passes_validation_and_fetches(self):
        # The normal path (test_normal_path_summarizes_and_posts) plus the
        # explicit contract: a well-formed https URL is NOT rejected and the
        # posted click_url is exactly the validated input.
        with _env(URL), \
                mock.patch.object(research.requests, "get",
                                  return_value=_Resp(ARTICLE_HTML)) as get, \
                mock.patch.object(research.summarize, "brief",
                                  return_value="A tidy summary."), \
                capture_pushes() as sent:
            research.run({})
        get.assert_called_once()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["click_url"], URL)
        self.assertNotIn("doesn't look like a URL", sent[0]["message"])

    def test_every_push_click_url_is_http_or_absent(self):
        # No research path may hand Discord an embed with an invalid click_url:
        # exercise the rejection, paywall, fetch-failure and summarizer-down
        # paths and check each posted reply's click_url is http(s) or absent.
        scenarios = [
            ("no scheme at all", None),
            (URL, _Resp(SHORT_HTML)),
            (URL, requests.RequestException("boom")),
        ]
        for env_url, response in scenarios:
            kwargs = ({"side_effect": response}
                      if isinstance(response, Exception) else
                      {"return_value": response})
            with self.subTest(env_url=env_url, response=response), _env(env_url), \
                    mock.patch.object(research.requests, "get", **kwargs), \
                    mock.patch.object(research.summarize, "brief", return_value=None), \
                    capture_pushes() as sent:
                research.run({})
            self.assertEqual(len(sent), 1)
            click = sent[0].get("click_url")
            if click is not None:
                self.assertTrue(click.startswith(("http://", "https://")),
                                f"invalid click_url would 400 on Discord: {click!r}")

    def test_empty_url_is_a_noop(self):
        with _env(""), \
                mock.patch.object(research.requests, "get") as get, \
                capture_pushes() as sent:
            state = research.run({})
        get.assert_not_called()
        self.assertEqual(sent, [])
        self.assertEqual(state, {})

    def test_short_extraction_posts_paywall_message(self):
        with _env(URL), \
                mock.patch.object(research.requests, "get",
                                  return_value=_Resp(SHORT_HTML)), \
                mock.patch.object(research.summarize, "brief") as brief, \
                capture_pushes() as sent:
            research.run({})
        brief.assert_not_called()
        self.assertEqual(len(sent), 1)
        self.assertIn("couldn't get the full article", sent[0]["message"])
        self.assertEqual(sent[0]["click_url"], URL)

    def test_normal_path_summarizes_and_posts(self):
        with _env(URL), \
                mock.patch.object(research.requests, "get",
                                  return_value=_Resp(ARTICLE_HTML)), \
                mock.patch.object(research.summarize, "brief",
                                  return_value="A tidy summary.") as brief, \
                capture_pushes() as sent:
            research.run({})

        brief.assert_called_once()
        system, text = brief.call_args[0]
        self.assertEqual(system, research.STYLE)
        self.assertIn("fact0", text)                    # article prose kept
        self.assertNotIn("Subscribe", text)             # nav boilerplate stripped
        self.assertLessEqual(len(text), research.MAX_CHARS)

        self.assertEqual(len(sent), 1)
        push = sent[0]
        self.assertEqual(push["message"], "A tidy summary.")
        self.assertEqual(push["title"], "Research: Chip Fab Expansion")
        self.assertEqual(push["click_url"], URL)
        self.assertEqual(push["topic"], "research")     # own rule (62); default channel

    def test_fetch_failure_posts_failure_message_without_raising(self):
        with _env(URL), \
                mock.patch.object(research.requests, "get",
                                  side_effect=requests.RequestException("boom")), \
                capture_pushes() as sent:
            research.run({})  # must not raise
        self.assertEqual(len(sent), 1)
        self.assertIn("couldn't summarize that link", sent[0]["message"])

    def test_summarizer_unavailable_posts_failure_message(self):
        # brief returns None when no provider key is set / every call failed.
        with _env(URL), \
                mock.patch.object(research.requests, "get",
                                  return_value=_Resp(ARTICLE_HTML)), \
                mock.patch.object(research.summarize, "brief", return_value=None), \
                capture_pushes() as sent:
            research.run({})
        self.assertEqual(len(sent), 1)
        self.assertIn("couldn't summarize that link", sent[0]["message"])


if __name__ == "__main__":
    unittest.main()
