"""Mission Control — the always-on Discord bot for notify-watcher.

This is the always-on companion to the scheduled ``notify_watcher`` sweep. It
reads the package read-only — the audit trail (``notify_watcher.audit``) for the
``!explain`` command and the topic→channel router (``notify_watcher.discord_delivery``)
for consistent embed coloring — and never touches the scraping logic or state.

It is also the always-on half of the **Discord-native control loop**: every
notification carries native reply buttons (Snooze / Mute / Read later …) whose
custom_id is ``nw|<command>``. This bot catches a tap and relays the bare command
to the private control channel (``DISCORD_CONTROL_CHANNEL``); the next scheduled
sweep drains that channel and applies it (see ``notify_watcher.discord_control``).
The bot is a pure courier — it never mutates state — so there is no race with the
runner, and a button keeps working across bot restarts. The scheduled watcher
keeps running exactly as before.

Run it with the project venv:

    Windows:        .venv\\Scripts\\python.exe bot.py
    macOS / Linux:  .venv/bin/python bot.py

It reads DISCORD_TOKEN from the .env file at the project root (gitignored, so
the token never reaches GitHub).

Note: the ``!ping`` command needs the *Message Content* privileged intent.
Enable it once at Discord Developer Portal -> Your App -> Bot -> "Message
Content Intent", otherwise the bot connects but can't read command text.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import discord
from discord.ext import commands
from dotenv import load_dotenv

from notify_watcher import audit, discord_control, discord_delivery

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")

# Load .env from the project root so DISCORD_TOKEN is available via os.getenv.
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# message_content is a privileged intent; it lets prefix commands like !ping
# actually read what the user typed.
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready() -> None:
    """Fired once the gateway handshake completes — proof we're connected."""
    print(f"Mission Control Online: Logged in as {bot.user}")
    log.info("Connected as %s (id=%s)", bot.user, getattr(bot.user, "id", "?"))


@bot.command()
async def ping(ctx: commands.Context) -> None:
    """Two-way comms test: `!ping` -> `Pong!`."""
    await ctx.send("Pong!")


# --- Reply-button control loop ----------------------------------------------
# Notifications delivered by the sweep carry native Discord buttons whose
# custom_id is ``nw|<command>`` (see notify_watcher.discord_control). This bot is
# the always-on half of the loop: it catches the button tap and relays the bare
# command into the control channel, where the next scheduled sweep drains it via
# discord_control.poll -> control.dispatch. The bot never touches state itself,
# so there is no race with the runner — it is a pure courier.

def _humanize_topic(slug: str) -> str:
    """``golden_sun`` -> ``Golden Sun`` for friendly acknowledgements."""
    return " ".join(p.capitalize() for p in slug.replace("-", "_").split("_") if p) or slug


def _ack_for(command: str) -> str:
    """Best-effort friendly confirmation for a relayed command (never raises)."""
    try:
        verb, _, rest = command.partition(":")
        parts = rest.split(":") if rest else []
        if verb == "MUTE" and len(parts) == 2:
            topic, hours = _humanize_topic(parts[0]), int(parts[1])
            if hours <= 1:
                return f"🔇 Snoozed **{topic}** for an hour."
            return f"🔇 Muted **{topic}** for {hours}h."
        if verb == "UNMUTE" and parts:
            return f"🔔 Unmuted **{_humanize_topic(parts[0])}**."
        if verb == "FOLLOW" and len(parts) == 2:
            return f"📌 Following **{_humanize_topic(parts[0])}** for {int(parts[1])}h."
        if verb == "UNFOLLOW" and parts:
            return f"Unfollowed **{_humanize_topic(parts[0])}**."
        if verb == "DONE":
            return "✅ Marked done — I'll skip the next nudge."
        if verb == "SNOOZE" and len(parts) == 2:
            return f"⏰ Snoozed — back in ~{int(parts[1])} min."
        if verb == "READ":
            return "🔖 Saved to your reading list."
        if verb == "MORE":
            return "🔎 I'll send the fuller story on the next sweep."
        if verb == "LATER" and len(parts) == 2:
            return f"⏰ I'll remind you in ~{int(parts[1])} min."
        if verb == "ADD":
            return "➕ Added."
        if verb == "IGNORE":
            return "🚫 Got it — I won't surface that again."
        if verb == "UNDO":
            return "↩️ Undone."
    except (ValueError, IndexError):
        pass
    return "Got it — I'll apply that on the next sweep."


async def _relay_command(command: str) -> None:
    """POST a bare command string into the control channel for the runner."""
    raw = (os.getenv("DISCORD_CONTROL_CHANNEL") or "").strip()
    if not raw or not raw.isdigit():
        raise RuntimeError("DISCORD_CONTROL_CHANNEL is not set to a channel id")
    channel = bot.get_channel(int(raw)) or await bot.fetch_channel(int(raw))
    await channel.send(command)


@bot.event
async def on_interaction(interaction: discord.Interaction) -> None:
    """Relay a reply-button tap (``nw|<command>``) to the control channel.

    Handled at the raw-interaction level rather than via a registered View, so a
    button keeps working after the bot restarts — nothing in memory has to
    survive, only the custom_id on the message. Non-``nw|`` interactions (e.g.
    slash commands) are ignored. Every failure path answers the user instead of
    leaving Discord's "interaction failed" spinner.
    """
    if interaction.type != discord.InteractionType.component:
        return
    custom_id = (interaction.data or {}).get("custom_id", "")
    if not custom_id.startswith(discord_control.CUSTOM_ID_PREFIX):
        return
    command = custom_id[len(discord_control.CUSTOM_ID_PREFIX):].strip()
    try:
        await _relay_command(command)
    except Exception:  # noqa: BLE001 - a bad tap must never take the bot down
        log.exception("failed to relay control command %r", command)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Sorry — I couldn't reach the control channel.", ephemeral=True)
        return
    log.info("relayed control command %r", command)
    await interaction.response.send_message(_ack_for(command), ephemeral=True)


def _fmt_ts(raw: object) -> str:
    """ISO timestamp -> 'Jun 13, 19:05 UTC' (best-effort, never raises)."""
    try:
        dt = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return str(raw or "unknown time")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%b %d, %H:%M UTC")


def _explain_embed(topic: str) -> discord.Embed:
    """Render the last few audited routing decisions for `topic` as an embed.

    Reads notify_watcher.audit (the same audit.json the sweep writes), which
    already tolerates a missing/corrupt file by returning an empty list — so the
    "no memory yet" branch covers both an absent file and an unseen topic.
    """
    items = audit.recent(topic)
    category = discord_delivery.category_for(topic)

    if not items:
        embed = discord.Embed(
            title=f"No memory yet for “{topic}”",
            description=(
                "I haven't recorded any routing decisions for this topic yet — "
                "either nothing has been dropped or deferred for it, or the name "
                "doesn't match a tracked topic.\n\n"
                "Try one like `fx`, `spending`, `movies`, `twitch`, or `games`."
            ),
            color=discord_delivery.CATEGORY_COLOR["general"],
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"explain · {category}")
        return embed

    embed = discord.Embed(
        title=f"🧠 Why I acted/stayed quiet on “{topic}”",
        description=f"The last {len(items)} routing decision(s), oldest first:",
        color=discord_delivery.color_for(topic),
        timestamp=datetime.now(timezone.utc),
    )
    for item in items:
        title = str(item.get("title") or "(untitled)")
        reason = str(item.get("reason") or "dropped by routing")
        source = str(item.get("source") or "").strip()
        score = item.get("score")
        meta = " · ".join(
            p for p in (
                source,
                f"score {score}" if score is not None else "",
                _fmt_ts(item.get("ts")),
            ) if p
        )
        value = f"_{reason}_" + (f"\n{meta}" if meta else "")
        embed.add_field(name=title[:256], value=value[:1024], inline=False)
    embed.set_footer(text=f"explain · {category} · {len(items)} kept")
    return embed


@bot.command(name="explain")
async def explain(ctx: commands.Context, topic: str = None) -> None:
    """`!explain <topic>` — show why the watcher recently acted or stayed quiet.

    Opens audit.json (via notify_watcher.audit), pulls the last few entries for
    the given topic, and replies with a formatted embed. Falls back to a polite
    "no memory yet" embed when the topic has never been logged.
    """
    if not topic:
        await ctx.send(
            "Usage: `!explain <topic>` — e.g. `!explain movies`, `!explain fx`."
        )
        return
    try:
        embed = _explain_embed(topic.strip().lower())
    except Exception:  # noqa: BLE001 - a command must never take the bot down
        log.exception("explain command failed for %r", topic)
        await ctx.send("Sorry — I hit an error reading the audit log.")
        return
    await ctx.send(embed=embed)


def main() -> None:
    if not TOKEN:
        raise SystemExit(
            "DISCORD_TOKEN is not set.\n"
            "Open the .env file at the project root and paste your bot token:\n"
            "    DISCORD_TOKEN=your-bot-token-here\n"
        )
    # bot.run installs its own signal handlers and reconnect loop, which is
    # what keeps this process alive 24/7.
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
