"""Tests for the "now streaming" check (notify_watcher.topics.movies.check_streaming)."""
from __future__ import annotations

import unittest
from unittest import mock

from notify_watcher.topics import movies
from tests._util import capture_pushes

SEARCH_JSON = {"results": [{"id": 603, "title": "The Matrix",
                            "release_date": "1999-03-31"}]}


def _providers_json(flatrate=(), rent=(), region="DO"):
    """A TMDb watch/providers payload with the given provider names."""
    block = {}
    if flatrate:
        block["flatrate"] = [{"provider_name": n} for n in flatrate]
    if rent:
        block["rent"] = [{"provider_name": n} for n in rent]
    return {"results": {region: block} if block else {}}


def _response(payload):
    resp = mock.Mock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


def _get_for(providers_payload, search_payload=SEARCH_JSON):
    """A fake requests.get dispatching on the two TMDb endpoints used."""
    def get(url, **kwargs):
        if "/watch/providers" in url:
            return _response(providers_payload)
        return _response(search_payload)
    return get


def _run(state, providers_payload=None, titles=("The Matrix",), get=None):
    """Run check_streaming with HTTP, watchlist, and config mocked.

    config.section returns {} so the priority engine is OFF and emit takes the
    legacy push path, which capture_pushes records.
    """
    if get is None:
        get = _get_for(providers_payload)
    with mock.patch.object(movies.requests, "get", get), \
         mock.patch.object(movies.watchlist, "titles", return_value=list(titles)), \
         mock.patch.object(movies.config, "section", return_value={}), \
         mock.patch.dict(movies.os.environ, {"TMDB_API_KEY": "k"}), \
         capture_pushes() as sent:
        state = movies.check_streaming(state)
    return state, sent


class CheckStreamingTest(unittest.TestCase):
    def test_first_run_seeds_silently(self):
        state, sent = _run({}, _providers_json(flatrate=["Netflix"]))
        self.assertEqual(sent, [])
        self.assertEqual(state[movies.STREAMING_STATE_KEY], {"603": ["Netflix"]})

    def test_new_provider_pushes_once(self):
        state = {movies.STREAMING_STATE_KEY: {"603": []}}
        state, sent = _run(state, _providers_json(flatrate=["Netflix"]))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["title"], "Movie: The Matrix")
        self.assertEqual(sent[0]["message"],
                         "🎬 The Matrix is now streaming on Netflix in DO")
        self.assertEqual(sent[0]["click_url"],
                         "https://www.themoviedb.org/movie/603")
        self.assertEqual(state[movies.STREAMING_STATE_KEY], {"603": ["Netflix"]})

    def test_same_provider_next_run_does_not_repush(self):
        state = {movies.STREAMING_STATE_KEY: {"603": ["Netflix"]}}
        state, sent = _run(state, _providers_json(flatrate=["Netflix"]))
        self.assertEqual(sent, [])
        self.assertEqual(state[movies.STREAMING_STATE_KEY], {"603": ["Netflix"]})

    def test_added_provider_alerts_only_the_new_one(self):
        state = {movies.STREAMING_STATE_KEY: {"603": ["Netflix"]}}
        state, sent = _run(state, _providers_json(flatrate=["Netflix", "Max"]))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["message"],
                         "🎬 The Matrix is now streaming on Max in DO")
        self.assertEqual(state[movies.STREAMING_STATE_KEY],
                         {"603": ["Max", "Netflix"]})

    def test_provider_that_left_and_returned_does_not_realert(self):
        # Seen-set is a union, so Netflix dropping the film (absent now) and
        # re-adding it later never produces a second "now streaming" push.
        state = {movies.STREAMING_STATE_KEY: {"603": ["Netflix"]}}
        state, sent = _run(state, _providers_json())
        self.assertEqual(sent, [])
        state, sent = _run(state, _providers_json(flatrate=["Netflix"]))
        self.assertEqual(sent, [])

    def test_no_do_region_seeds_empty_and_stays_quiet(self):
        state, sent = _run({}, _providers_json())
        self.assertEqual(sent, [])
        self.assertEqual(state[movies.STREAMING_STATE_KEY], {"603": []})

    def test_rent_only_listing_does_not_count_as_streaming(self):
        state = {movies.STREAMING_STATE_KEY: {"603": []}}
        state, sent = _run(state, _providers_json(rent=["Apple TV"]))
        self.assertEqual(sent, [])
        self.assertEqual(state[movies.STREAMING_STATE_KEY], {"603": []})

    def test_one_failed_lookup_does_not_block_the_rest(self):
        good_get = _get_for(_providers_json(flatrate=["Netflix"]))

        def get(url, params=None, **kwargs):
            if (params or {}).get("query") == "Broken Movie":
                raise RuntimeError("connection refused")
            return good_get(url, params=params, **kwargs)

        state = {movies.STREAMING_STATE_KEY: {"603": []}}
        state, sent = _run(state, titles=("Broken Movie", "The Matrix"), get=get)
        self.assertEqual(len(sent), 1)
        self.assertIn("The Matrix is now streaming on Netflix", sent[0]["message"])

    def test_no_api_key_is_a_noop(self):
        get = mock.Mock(side_effect=AssertionError("must not hit the network"))
        with mock.patch.object(movies.requests, "get", get), \
             mock.patch.dict(movies.os.environ, {"TMDB_API_KEY": ""}), \
             capture_pushes() as sent:
            state = movies.check_streaming({})
        self.assertEqual(sent, [])
        self.assertNotIn(movies.STREAMING_STATE_KEY, state)


if __name__ == "__main__":
    unittest.main()
