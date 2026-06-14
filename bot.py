"""Mission Control — the always-on Discord bot for notify-watcher.

This is the always-on companion to the scheduled ``notify_watcher`` sweep. It
reads the package read-only — the audit trail (``notify_watcher.audit``) for the
``!explain`` command and the topic→channel router (``notify_watcher.discord_delivery``)
for consistent embed coloring — and never touches the scraping logic. The
scheduled watcher keeps running exactly as before.

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

from notify_watcher import audit, discord_delivery

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


class NotificationActionView(discord.ui.View):
    """Temporary Discord-native action buttons for manual UI testing."""

    async def _ack(
        self,
        interaction: discord.Interaction,
        action: str,
        message: str,
    ) -> None:
        print(f"Discord UI action selected: {action}")
        await interaction.response.send_message(message, ephemeral=True)

    @discord.ui.button(label="Snooze 1H", style=discord.ButtonStyle.primary)
    async def snooze_1h(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self._ack(interaction, "snooze_1h", "Snoozed for 1 hour.")

    @discord.ui.button(label="Mute Topic", style=discord.ButtonStyle.danger)
    async def mute_topic(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self._ack(interaction, "mute_topic", "Topic muted.")

    @discord.ui.button(label="Acknowledge", style=discord.ButtonStyle.secondary)
    async def acknowledge(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self._ack(interaction, "acknowledge", "Acknowledged.")


@bot.command(name="testbuttons")
async def testbuttons(ctx: commands.Context) -> None:
    """Spawn a disposable Discord UI component test panel."""
    await ctx.send(
        "Discord UI component test:",
        view=NotificationActionView(timeout=300),
    )


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
