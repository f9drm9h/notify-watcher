"""Tests for the Discord-native control loop (notify_watcher.discord_control).

Pure stdlib unittest, no network: the channel poll is exercised by patching
requests.get with a canned messages payload, the button conversion and command
extraction are pure, and the enabled/disabled kill switch is driven by the
environment.
"""
from __future__ import annotations

import unittest
from unittest import mock

from notify_watcher import discord_control as dc

# A configured control loop: a bot token + a numeric control channel id.
ENV = {"DISCORD_TOKEN": "tok", "DISCORD_CONTROL_CHANNEL": "555"}
EID = "ab12cd34ef56ab78"  # a valid 16-hex event-log id


def _env(**overrides):
    base = {"DISCORD_TOKEN": "", "DISCORD_CONTROL_CHANNEL": ""}
    base.update(overrides)
    return mock.patch.dict("os.environ", base, clear=True)


class EnabledTest(unittest.TestCase):
    def test_needs_both_token_and_channel(self):
        with _env(**ENV):
            self.assertTrue(dc.enabled())
        with _env(DISCORD_TOKEN="tok"):
            self.assertFalse(dc.enabled())  # no channel
        with _env(DISCORD_CONTROL_CHANNEL="555"):
            self.assertFalse(dc.enabled())  # no token
        with _env():
            self.assertFalse(dc.enabled())


class ComponentTest(unittest.TestCase):
    def test_descriptors_render_as_one_action_row(self):
        with _env(**ENV):
            rows = dc.actions_to_components([
                {"label": "Mute 24h", "command": "MUTE:movies:24"},
                {"label": "Read later", "command": f"READ:{EID}"},
            ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["type"], 1)  # action row
        buttons = rows[0]["components"]
        self.assertEqual([b["custom_id"] for b in buttons],
                         ["nw|MUTE:movies:24", f"nw|READ:{EID}"])
        self.assertEqual([b["label"] for b in buttons], ["Mute 24h", "Read later"])

    def test_mute_is_danger_styled_others_secondary(self):
        with _env(**ENV):
            rows = dc.actions_to_components([
                {"label": "Mute 24h", "command": "MUTE:movies:24"},
                {"label": "Read later", "command": f"READ:{EID}"},
                {"label": "Done", "command": "DONE:water"},
            ])
        styles = [b["style"] for b in rows[0]["components"]]
        self.assertEqual(styles, [dc._STYLE_DANGER, dc._STYLE_SECONDARY,
                                  dc._STYLE_SUCCESS])

    def test_disabled_renders_nothing(self):
        with _env():  # kill switch: no channel configured
            self.assertIsNone(dc.actions_to_components(
                [{"label": "Mute", "command": "MUTE:movies:24"}]))

    def test_empty_or_commandless_lists_render_nothing(self):
        with _env(**ENV):
            self.assertIsNone(dc.actions_to_components([]))
            self.assertIsNone(dc.actions_to_components([{"label": "x"}]))

    def test_legacy_body_descriptor_still_renders(self):
        # Defensive: an old ntfy-shaped descriptor carried the command in "body".
        with _env(**ENV):
            rows = dc.actions_to_components([{"label": "Done", "body": "DONE:water"}])
        self.assertEqual(rows[0]["components"][0]["custom_id"], "nw|DONE:water")

    def test_row_is_capped_at_max_buttons(self):
        with _env(**ENV):
            rows = dc.actions_to_components(
                [{"label": str(i), "command": f"MUTE:t{i}:1"} for i in range(8)])
        self.assertEqual(len(rows[0]["components"]), dc.MAX_BUTTONS)


class ExtractCommandsTest(unittest.TestCase):
    MSGS = [  # Discord returns newest-first
        {"id": "300", "content": "MUTE:movies:24"},
        {"id": "200", "content": "status fx"},
        {"id": "100", "content": "old already-seen"},
    ]

    def test_returns_only_newer_oldest_first_and_advances_cursor(self):
        commands, cursor = dc.extract_commands(self.MSGS, "100")
        self.assertEqual(commands, ["status fx", "MUTE:movies:24"])  # oldest-first
        self.assertEqual(cursor, "300")

    def test_no_new_messages_keeps_cursor(self):
        commands, cursor = dc.extract_commands(
            [{"id": "100", "content": "old"}], "100")
        self.assertEqual(commands, [])
        self.assertEqual(cursor, "100")

    def test_skips_malformed_and_blank(self):
        msgs = [{"id": "200", "content": "  "}, {"id": "x", "content": "bad id"},
                "nope", {"content": "no id"}, {"id": "201", "content": "ok"}]
        commands, cursor = dc.extract_commands(msgs, "100")
        self.assertEqual(commands, ["ok"])
        self.assertEqual(cursor, "201")

    def test_non_list_payload_is_safe(self):
        self.assertEqual(dc.extract_commands({"error": "nope"}, "100"), ([], "100"))

    def test_caps_at_max_per_poll(self):
        msgs = [{"id": str(1000 + i), "content": f"c{i}"}
                for i in range(dc.MAX_PER_POLL + 10)]
        commands, _ = dc.extract_commands(msgs, "0")
        self.assertEqual(len(commands), dc.MAX_PER_POLL)


class PollTest(unittest.TestCase):
    def _resp(self, payload):
        resp = mock.Mock()
        resp.json.return_value = payload
        resp.raise_for_status = mock.Mock()
        return resp

    def test_disabled_skips_network(self):
        with _env(), mock.patch.object(dc.requests, "get") as get:
            self.assertEqual(dc.poll({}), [])
        get.assert_not_called()

    def test_first_run_seeds_cursor_without_dispatching(self):
        state: dict = {}
        with _env(**ENV), mock.patch.object(
                dc.requests, "get",
                return_value=self._resp([{"id": "900", "content": "MUTE:movies:24"}])) as get:
            commands = dc.poll(state)
        self.assertEqual(commands, [])  # backlog never replayed
        self.assertEqual(state["discord_control"]["last_id"], "900")
        # First poll asks for just the latest message to seed the cursor.
        self.assertEqual(get.call_args.kwargs["params"]["limit"], 1)

    def test_subsequent_poll_returns_new_commands_and_advances(self):
        state = {"discord_control": {"last_id": "100"}}
        with _env(**ENV), mock.patch.object(
                dc.requests, "get",
                return_value=self._resp([{"id": "300", "content": "MUTE:movies:24"},
                                         {"id": "200", "content": "status fx"}])) as get:
            commands = dc.poll(state)
        self.assertEqual(commands, ["status fx", "MUTE:movies:24"])
        self.assertEqual(state["discord_control"]["last_id"], "300")
        self.assertEqual(get.call_args.kwargs["params"]["after"], "100")

    def test_network_error_returns_empty_and_keeps_cursor(self):
        state = {"discord_control": {"last_id": "100"}}
        with _env(**ENV), mock.patch.object(
                dc.requests, "get", side_effect=OSError("boom")):
            self.assertEqual(dc.poll(state), [])
        self.assertEqual(state["discord_control"]["last_id"], "100")


class DispatchReuseTest(unittest.TestCase):
    """The whole point: polled Discord commands flow into control.dispatch."""

    def test_polled_mute_command_mutes_via_control_dispatch(self):
        from notify_watcher import control
        state = {"discord_control": {"last_id": "100"}}
        resp = mock.Mock()
        resp.json.return_value = [{"id": "200", "content": "MUTE:movies:24"}]
        resp.raise_for_status = mock.Mock()
        with _env(**ENV), mock.patch.object(dc.requests, "get", return_value=resp):
            control.dispatch(dc.poll(state), state)
        self.assertIn("movies", state.get(control.MUTED_KEY, {}))


if __name__ == "__main__":
    unittest.main()
