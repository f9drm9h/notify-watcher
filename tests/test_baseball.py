"""Tests for the Dominican baseball watcher (notify_watcher.topics.baseball)."""
from __future__ import annotations

import unittest
from datetime import date
from unittest import mock

from notify_watcher.topics import baseball
from tests._util import capture_pushes

TODAY = date(2026, 6, 10)
YESTERDAY = date(2026, 6, 9)

TEAM_CFG = {"team_id": 119, "team_name": "Dodgers"}
PLAYER_CFG = {"dominican_players": [
    {"person_id": 665489, "name": "Vladimir Guerrero Jr."},
]}


def _side(team_id, name, score=None):
    d = {"team": {"id": team_id, "name": name, "teamName": name}}
    if score is not None:
        d["score"] = score
    return d


def _game(pk=1001, status="Final", home=None, away=None):
    return {
        "gamePk": pk,
        "status": {"abstractGameState": status},
        "teams": {"home": home or _side(119, "Dodgers", 5),
                  "away": away or _side(112, "Cubs", 3)},
    }


def _schedule_payload(games):
    return {"dates": [{"games": games}]} if games else {"dates": []}


def _split(pk=555, day="2026-06-10", hr=0, hits=0, rbi=0, opp="Yankees"):
    return {
        "date": day,
        "game": {"gamePk": pk},
        "opponent": {"id": 147, "name": opp, "teamName": opp},
        "stat": {"homeRuns": hr, "hits": hits, "rbi": rbi},
    }


def _gamelog_payload(splits):
    return {"stats": [{"splits": splits}]} if splits is not None else {"stats": []}


def _response(payload):
    resp = mock.Mock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


def _get_for(schedule_payload=None, gamelog_payload=None):
    """A fake requests.get dispatching on the two MLB Stats endpoints used."""
    def get(url, **kwargs):
        if "/schedule" in url:
            return _response(schedule_payload or _schedule_payload([]))
        return _response(gamelog_payload or _gamelog_payload(None))
    return get


def _check_result(state, schedule_payload, cfg=TEAM_CFG):
    """Run _check_team_result with HTTP and config mocked.

    config.section returns {} so the priority engine is OFF and emit takes the
    legacy path: severity-low results buffer into state["digest_buffer"].
    """
    with mock.patch.object(baseball.requests, "get", _get_for(schedule_payload)), \
         mock.patch.object(baseball.config, "section", return_value={}), \
         capture_pushes() as sent:
        state = baseball._check_team_result(state, cfg, YESTERDAY)
    return state, sent


def _check_milestones(state, gamelog_payload, cfg=PLAYER_CFG):
    """Run _check_milestones with HTTP and config mocked (legacy push path)."""
    with mock.patch.object(baseball.requests, "get",
                           _get_for(gamelog_payload=gamelog_payload)), \
         mock.patch.object(baseball.config, "section", return_value={}), \
         capture_pushes() as sent:
        state = baseball._check_milestones(state, cfg, TODAY)
    return state, sent


class ResultLineTest(unittest.TestCase):
    def test_win(self):
        line = baseball._result_line(_game(), 119, "Dodgers")
        self.assertEqual(line, "Dodgers 5 – Cubs 3 (W)")

    def test_loss_with_team_on_away_side(self):
        g = _game(home=_side(112, "Cubs", 6), away=_side(119, "Dodgers", 2))
        self.assertEqual(baseball._result_line(g, 119, "Dodgers"),
                         "Dodgers 2 – Cubs 6 (L)")

    def test_in_progress_game_is_none(self):
        self.assertIsNone(baseball._result_line(_game(status="Live"), 119, "Dodgers"))

    def test_other_teams_game_is_none(self):
        self.assertIsNone(baseball._result_line(_game(), 121, "Mets"))

    def test_missing_score_is_none(self):
        g = _game(home=_side(119, "Dodgers"), away=_side(112, "Cubs"))
        self.assertIsNone(baseball._result_line(g, 119, "Dodgers"))


class TeamResultTest(unittest.TestCase):
    def test_final_game_lands_in_digest_once(self):
        state, sent = _check_result({}, _schedule_payload([_game()]))
        self.assertEqual(sent, [])  # digested, not pushed
        buf = state.get("digest_buffer") or []
        self.assertEqual(len(buf), 1)
        self.assertEqual(buf[0]["title"], "Dodgers 5 – Cubs 3 (W)")
        self.assertEqual(state[baseball.RESULTS_SEEN_KEY], [1001])

    def test_off_day_is_silent(self):
        state, sent = _check_result({}, _schedule_payload([]))
        self.assertEqual(sent, [])
        self.assertNotIn("digest_buffer", state)

    def test_in_progress_game_is_skipped_and_not_marked_seen(self):
        state, sent = _check_result({}, _schedule_payload([_game(status="Live")]))
        self.assertEqual(sent, [])
        self.assertNotIn("digest_buffer", state)
        # pk stays unrecorded so the game can still report once it finalizes
        self.assertEqual(state[baseball.RESULTS_SEEN_KEY], [])

    def test_seen_game_is_not_redigested(self):
        state = {baseball.RESULTS_SEEN_KEY: [1001]}
        state, sent = _check_result(state, _schedule_payload([_game()]))
        self.assertEqual(sent, [])
        self.assertNotIn("digest_buffer", state)

    def test_monitored_teams_each_get_a_line(self):
        payloads = {
            141: _schedule_payload([_game(pk=2001,
                                          home=_side(141, "Blue Jays", 4),
                                          away=_side(110, "Orioles", 2))]),
            119: _schedule_payload([_game(pk=2002)]),
        }

        def get(url, params=None, **kwargs):
            return _response(payloads[params["teamId"]])

        cfg = {"monitored_teams": [
            {"team_id": 141, "team_name": "Blue Jays"},
            {"team_id": 119, "team_name": "Dodgers"},
        ]}
        with mock.patch.object(baseball.requests, "get", get), \
             mock.patch.object(baseball.config, "section", return_value={}), \
             capture_pushes() as sent:
            state = baseball._check_team_result({}, cfg, YESTERDAY)
        self.assertEqual(sent, [])
        titles = [e["title"] for e in (state.get("digest_buffer") or [])]
        self.assertEqual(titles, ["Blue Jays 4 – Orioles 2 (W)",
                                  "Dodgers 5 – Cubs 3 (W)"])
        self.assertEqual(state[baseball.RESULTS_SEEN_KEY], [2001, 2002])


class MilestonePartsTest(unittest.TestCase):
    def test_homer_and_rbi(self):
        parts = baseball._milestone_parts({"homeRuns": 2, "hits": 2, "rbi": 4})
        self.assertEqual(parts, ["2 HR", "4 RBI"])

    def test_three_hits(self):
        self.assertEqual(baseball._milestone_parts({"hits": 3}), ["3 hits"])

    def test_quiet_night_is_empty(self):
        self.assertEqual(baseball._milestone_parts({"homeRuns": 0, "hits": 2, "rbi": 1}), [])


class MilestoneAlertTest(unittest.TestCase):
    def test_milestone_pushes_once(self):
        payload = _gamelog_payload([_split(hr=2, hits=2, rbi=4)])
        state, sent = _check_milestones({}, payload)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["title"], "Baseball: Vladimir Guerrero Jr.")
        self.assertEqual(sent[0]["message"],
                         "🇩🇴 Vladimir Guerrero Jr. — 2 HR, 4 RBI vs Yankees")
        self.assertEqual(state[baseball.MILESTONES_SEEN_KEY], [555])

    def test_no_milestone_is_silent(self):
        payload = _gamelog_payload([_split(hr=0, hits=1, rbi=1)])
        state, sent = _check_milestones({}, payload)
        self.assertEqual(sent, [])
        self.assertEqual(state[baseball.MILESTONES_SEEN_KEY], [])

    def test_seen_game_pk_is_not_realerted(self):
        payload = _gamelog_payload([_split(hr=2, hits=2, rbi=4)])
        state = {baseball.MILESTONES_SEEN_KEY: [555]}
        state, sent = _check_milestones(state, payload)
        self.assertEqual(sent, [])
        self.assertEqual(state[baseball.MILESTONES_SEEN_KEY], [555])

    def test_other_days_games_are_ignored(self):
        payload = _gamelog_payload([_split(day="2026-06-01", hr=3, hits=4, rbi=6)])
        state, sent = _check_milestones({}, payload)
        self.assertEqual(sent, [])

    def test_one_failed_player_does_not_block_the_rest(self):
        good = _get_for(gamelog_payload=_gamelog_payload([_split(hr=1)]))

        def get(url, **kwargs):
            if "/people/111/" in url:
                raise RuntimeError("connection refused")
            return good(url, **kwargs)

        cfg = {"dominican_players": [
            {"person_id": 111, "name": "Broken Player"},
            {"person_id": 665489, "name": "Vladimir Guerrero Jr."},
        ]}
        with mock.patch.object(baseball.requests, "get", get), \
             mock.patch.object(baseball.config, "section", return_value={}), \
             capture_pushes() as sent:
            baseball._check_milestones({}, cfg, TODAY)
        self.assertEqual(len(sent), 1)
        self.assertIn("Vladimir Guerrero Jr.", sent[0]["message"])


class OffSeasonTest(unittest.TestCase):
    def test_no_games_for_a_week_skips_everything(self):
        cfg = {**TEAM_CFG, **PLAYER_CFG}

        def section(name):
            return cfg if name == "baseball" else {}

        def get(url, **kwargs):
            if "/schedule" in url:
                return _response(_schedule_payload([]))
            raise AssertionError("off-season must not fetch game logs")

        with mock.patch.object(baseball.requests, "get", get), \
             mock.patch.object(baseball.config, "section", side_effect=section), \
             mock.patch.dict(baseball.os.environ, {"NOTIFY_DAILY": "1"}), \
             capture_pushes() as sent:
            state = baseball.run({})
        self.assertEqual(sent, [])
        self.assertEqual(state, {})


if __name__ == "__main__":
    unittest.main()
