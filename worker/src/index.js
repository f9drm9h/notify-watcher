/**
 * notify-watcher — Discord Interactions Worker
 * ------------------------------------------------------------------
 * This file REPLACES the always-on bot.py. Instead of a Python process
 * that stays connected to Discord forever, Discord sends each slash
 * command and button tap here as a single HTTPS request. The Worker
 * runs for a fraction of a second, does its job, and stops. Nothing
 * stays online. Nothing for you to keep alive.
 *
 * It keeps bot.py's core idea: this code is a COURIER. It never edits
 * state.json. For commands that change something (mute, follow, done),
 * it posts the bare command into the private Discord control channel,
 * exactly like bot.py did, and the next GitHub Actions sweep applies it.
 * For commands that only READ (status, explain), it fetches the latest
 * state.json straight from the public GitHub repo and formats a reply.
 *
 * WHAT YOU MUST SET (as Worker "secrets" / vars, never hard-coded here):
 *   DISCORD_PUBLIC_KEY     - from Discord Developer Portal > your app > General
 *   DISCORD_BOT_TOKEN      - your existing bot token (same as DISCORD_TOKEN)
 *   DISCORD_CONTROL_CHANNEL- the private control channel id
 *   STATE_BASE_URL         - e.g. https://raw.githubusercontent.com/<user>/<repo>/<branch>
 *   GITHUB_TOKEN           - (Phase 3 only) fine-grained token, Actions read+write
 *   GITHUB_DISPATCH_URL    - (Phase 3 only) the workflow_dispatch API url
 *
 * Discord interaction types:   1 = PING, 2 = SLASH COMMAND, 3 = BUTTON/COMPONENT
 * Discord response types:      1 = PONG, 4 = REPLY (with flags 64 = only-you-see-it)
 *
 * This is a STARTING SKELETON. The owning agent finishes the TODOs and
 * tests every command against the real Discord app.
 */

const PONG = { type: 1 };
const EPHEMERAL = 64; // reply visible only to the person who tapped

// ----- entry point ------------------------------------------------------
export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("notify-watcher worker is alive", { status: 200 });
    }

    // 1. Read the raw body ONCE. Signature check needs the exact bytes.
    const body = await request.text();

    // 2. Reject anything that is not a genuine Discord request.
    const ok = await verifyDiscordSignature(request, body, env.DISCORD_PUBLIC_KEY);
    if (!ok) return new Response("bad signature", { status: 401 });

    const interaction = JSON.parse(body);

    // 3. Discord's handshake: it pings the endpoint to confirm it works.
    if (interaction.type === 1) return json(PONG);

    try {
      if (interaction.type === 2) return await handleSlashCommand(interaction, env);
      if (interaction.type === 3) return await handleButton(interaction, env);
    } catch (err) {
      // Never leave Discord's "interaction failed" spinner hanging.
      return reply("Something went wrong handling that. Try again shortly.");
    }
    return reply("Unsupported interaction.");
  },
};

// ----- slash commands ---------------------------------------------------
async function handleSlashCommand(interaction, env) {
  const name = interaction.data?.name;
  const opts = optionMap(interaction.data?.options);

  switch (name) {
    case "ping":
      return reply("Pong.");

    // --- ACTION commands: relay to the control channel (courier role) ---
    case "mute": // /mute topic:games hours:24  ->  MUTE:games:24
      return await relay(`MUTE:${opts.topic}:${opts.hours ?? 24}`, env,
        ackMute(opts.topic, opts.hours ?? 24));
    case "unmute": // /unmute topic:games  ->  UNMUTE:games
      return await relay(`UNMUTE:${opts.topic}`, env, `Unmuted ${human(opts.topic)}.`);
    case "follow": // /follow topic:movies hours:48
      return await relay(`FOLLOW:${opts.topic}:${opts.hours ?? 24}`, env,
        `Following ${human(opts.topic)} for ${opts.hours ?? 24}h.`);
    case "unfollow":
      return await relay(`UNFOLLOW:${opts.topic}`, env, `Unfollowed ${human(opts.topic)}.`);

    // --- READ commands: fetch state from GitHub, reply directly ---------
    case "status":
      return await cmdStatus(opts.topic, env);
    case "explain":
      return await cmdExplain(opts.topic, env);

    // --- ON-DEMAND run (Phase 3): trigger a GitHub Actions sweep --------
    case "run":
      // TODO (B6): POST to GITHUB_DISPATCH_URL with the new "only" input so
      // the sweep runs just opts.topic and posts a fresh result.
      return reply("On-demand run is not wired up yet.");

    default:
      return reply(`Unknown command: ${name}`);
  }
}

// ----- buttons (the direct bot.py port) ---------------------------------
// Notification buttons carry a custom_id of "nw|<command>". We strip the
// prefix and relay the bare command, exactly as bot.py did.
async function handleButton(interaction, env) {
  const customId = interaction.data?.custom_id || "";
  const PREFIX = "nw|";
  if (!customId.startsWith(PREFIX)) return reply("That button is not for me.");
  const command = customId.slice(PREFIX.length).trim();
  return await relay(command, env, ackFor(command));
}

// ----- the courier: post a bare command into the control channel --------
async function relay(command, env, ackText) {
  const channel = env.DISCORD_CONTROL_CHANNEL;
  const res = await fetch(`https://discord.com/api/v10/channels/${channel}/messages`, {
    method: "POST",
    headers: {
      Authorization: `Bot ${env.DISCORD_BOT_TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ content: command }),
  });
  if (!res.ok) return reply("Could not reach the control channel.");
  return reply(ackText || "Got it. I'll apply that on the next sweep.");
}

// ----- a read command example: /status ----------------------------------
// Reads the latest committed state.json from the PUBLIC repo (no token).
async function cmdStatus(topic, env) {
  const state = await fetchState(env);
  if (!state) return reply("Could not read the latest state right now.");

  if (!topic) {
    const last = state.last_run || {};
    return reply(
      `Last sweep: ${last.ok ?? "?"} ok, ${last.failed ?? "?"} failed (${last.ts ?? "unknown"}).`
    );
  }
  // NOTE (Phase 0 trap): muted topics live in state.muted; the live control
  // cursor is state.discord_control.last_id, NOT state.control (that one is the
  // dormant legacy ntfy cursor). And state.follows is the artist/streamer
  // overlay, while a followed TOPIC would live under state.followed.
  const muted = state.muted?.[topic];
  const health = state.topic_health?.[topic]?.last_ok;
  const lines = [
    `Status for ${human(topic)}:`,
    muted ? `Muted until ${muted}.` : "Not muted.",
    health ? `Last ran OK at ${health}.` : "No recent run recorded.",
  ];
  return reply(lines.join("\n"));
}

async function cmdExplain(topic, env) {
  const audit = await fetchRepoJson(env, "audit.json");
  if (!audit) return reply("Could not read the audit log right now.");

  const key = String(topic || "").trim().toLowerCase();
  const items = Array.isArray(audit[key]) ? audit[key].slice(-5) : [];

  if (items.length === 0) {
    return reply(
      `No memory yet for ${human(key)}. I haven't recorded any routing ` +
      `decisions for it, or the name doesn't match a tracked topic. ` +
      `Try one like movies, fx, spending, twitch, or games.`
    );
  }

  const lines = [`Why I acted or stayed quiet on ${human(key)} (last ${items.length}, oldest first):`];
  for (const it of items) {
    const title = String(it.title || "(untitled)").slice(0, 140);
    const reason = String(it.reason || "dropped by routing");
    const meta = [
      String(it.source || "").trim(),
      it.score != null ? `score ${it.score}` : "",
      fmtTs(it.ts),
    ].filter(Boolean).join(" \u00b7 ");
    lines.push("", `\u2022 ${title}`, `  ${reason}`);
    if (meta) lines.push(`  ${meta}`);
  }
  return reply(lines.join("\n").slice(0, 1900));
}

async function fetchRepoJson(env, name) {
  try {
    const res = await fetch(`${env.STATE_BASE_URL}/${name}`, { cf: { cacheTtl: 30 } });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}
async function fetchState(env) {
  return await fetchRepoJson(env, "state.json");
}

// ----- friendly acknowledgements (ported from bot.py _ack_for) ----------
function ackMute(topic, hours) {
  if (Number(hours) <= 1) return `Snoozed ${human(topic)} for an hour.`;
  return `Muted ${human(topic)} for ${hours}h.`;
}

function ackFor(command) {
  const [verb, ...rest] = command.split(":");
  const a = rest[0];
  const b = rest[1];
  switch (verb) {
    case "MUTE": return ackMute(a, b);
    case "UNMUTE": return `Unmuted ${human(a)}.`;
    case "FOLLOW": return `Following ${human(a)} for ${b}h.`;
    case "UNFOLLOW": return `Unfollowed ${human(a)}.`;
    case "DONE": return "Marked done. I'll skip the next nudge.";
    case "READ": return "Saved to your reading list.";
    case "MORE": return "I'll send the fuller story on the next sweep.";
    case "LATER": return `I'll remind you in about ${b} minutes.`;
    case "IGNORE": return "Got it. I won't surface that again.";
    case "UNDO": return "Undone.";
    default: return "Got it. I'll apply that on the next sweep.";
  }
}

// ----- small helpers ----------------------------------------------------
function human(slug) {
  if (!slug) return "that topic";
  return slug.replace(/-/g, "_").split("_").filter(Boolean)
    .map((p) => p[0].toUpperCase() + p.slice(1)).join(" ");
}

function fmtTs(raw) {
  const d = new Date(raw);
  if (isNaN(d.getTime())) return String(raw || "unknown time");
  const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  const p = (n) => String(n).padStart(2, "0");
  return `${months[d.getUTCMonth()]} ${p(d.getUTCDate())}, ${p(d.getUTCHours())}:${p(d.getUTCMinutes())} UTC`;
}

function optionMap(options) {
  const out = {};
  for (const o of options || []) out[o.name] = o.value;
  return out;
}

function reply(content) {
  return json({ type: 4, data: { content, flags: EPHEMERAL } });
}

function json(obj) {
  return new Response(JSON.stringify(obj), {
    headers: { "Content-Type": "application/json" },
  });
}

// ----- Ed25519 signature verification -----------------------------------
// Discord signs every request. We must confirm it before trusting it.
// Uses the Workers built-in Ed25519 support (no npm package needed). If a
// deploy ever rejects the algorithm name, swap to the "discord-interactions"
// package's verifyKey, which wrangler will bundle.
async function verifyDiscordSignature(request, body, publicKeyHex) {
  const signature = request.headers.get("x-signature-ed25519");
  const timestamp = request.headers.get("x-signature-timestamp");
  if (!signature || !timestamp || !publicKeyHex) return false;
  try {
    const key = await crypto.subtle.importKey(
      "raw", hexToBytes(publicKeyHex), { name: "Ed25519" }, false, ["verify"]
    );
    return await crypto.subtle.verify(
      { name: "Ed25519" }, key,
      hexToBytes(signature),
      new TextEncoder().encode(timestamp + body)
    );
  } catch {
    return false;
  }
}

function hexToBytes(hex) {
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < bytes.length; i++) {
    bytes[i] = parseInt(hex.substr(i * 2, 2), 16);
  }
  return bytes;
}
 
