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
 *   GITHUB_DISPATCH_TOKEN  - token for the CADENCE cron below (scheduled()).
 *                            Set with: wrangler secret put GITHUB_DISPATCH_TOKEN
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

  // ----- the watch.yml cadence (replaces GitHub's schedule trigger) -------
  // GitHub's shared-queue cron delayed or dropped most 15-minute ticks
  // (116-min average gap vs the configured 15, measured Jul 6-12 2026), so
  // the Cron Trigger in wrangler.toml fires here instead and dispatches the
  // workflow directly. scheduled:"true" tells watch.yml to keep the old
  // schedule behavior (twitch fast lane + windowed full sweep) instead of
  // treating the dispatch as a full manual run.
  //
  // This handler must NEVER throw: a failed dispatch is logged (visible in
  // `wrangler tail` and the dashboard's Worker logs) and the next tick, 15
  // minutes later, simply tries again.
  async scheduled(event, env, ctx) {
    try {
      if (!env.GITHUB_DISPATCH_TOKEN) {
        console.error(
          "cadence dispatch skipped: GITHUB_DISPATCH_TOKEN secret is not set " +
          "(run: wrangler secret put GITHUB_DISPATCH_TOKEN)"
        );
        return;
      }
      const res = await fetch(env.GITHUB_DISPATCH_URL, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GITHUB_DISPATCH_TOKEN}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "notify-watcher-worker",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ ref: "main", inputs: { scheduled: "true" } }),
      });
      if (res.status !== 204) {
        // GitHub answers 204 No Content on success; anything else is a real
        // failure (401 bad/expired token, 404 wrong repo/workflow or a token
        // without Actions write, 422 bad ref/inputs). Log status AND body —
        // the body carries GitHub's human-readable reason.
        const body = await res.text();
        console.error(
          `cadence dispatch failed: HTTP ${res.status} ${res.statusText} — ` +
          body.slice(0, 500)
        );
      }
    } catch (err) {
      console.error(`cadence dispatch error: ${err}`);
    }

    // The external heartbeat runs AFTER the dispatch and independently of
    // whether it succeeded — a failed dispatch is itself something the
    // heartbeat should end up reporting.
    try {
      await externalHeartbeat(env);
    } catch (err) {
      console.error(`external heartbeat error: ${err}`);
    }
  },
};

// ----- the external heartbeat (the only monitor GitHub cannot take down) ---
//
// Everything in .github/workflows/alert.yml runs ON GitHub Actions, which makes
// it structurally unable to report an outage that stops GitHub Actions. That is
// not a theoretical gap. During the July 2026 billing lockout, `watch` failed
// 449 times and every one of the 468 alert runs meant to report those failures
// was refused before its first step ran. Total failures, zero notifications.
//
// This Worker is the only piece of the system hosted somewhere else, and it
// already wakes up every 15 minutes, so it is the natural place for a check
// that survives GitHub being unusable: ask when `watch` last SUCCEEDED, and if
// that was too long ago, post straight to Discord.
//
// Deliberate design limits, so this stays a safety net and not a second system:
//   - READ-ONLY on GitHub, one extra API call per hour.
//   - No state. Workers have no durable storage here, so instead of tracking an
//     outage the check only runs on the first cron tick of each hour (:07),
//     which caps it at one reminder per hour for as long as the outage lasts.
//     That is the reminder cadence, achieved by pacing rather than bookkeeping.
//   - No recovery notice. Announcing recovery needs memory of the outage, and
//     once GitHub is back alert.yml's own on-recovery job sends one anyway.
//   - Never throws, exactly like the dispatch above: a broken heartbeat must not
//     be able to break the cadence that keeps the whole watcher running.
//
// Requires DISCORD_BOT_TOKEN (already set) and CHANNEL_LOGS. Without the
// channel id it logs once and does nothing, so an un-migrated deploy degrades
// to today's behavior instead of erroring every 15 minutes.
const HEARTBEAT_STALE_HOURS = 7;   // ~28 missed 15-minute ticks
const HEARTBEAT_TICK_MINUTE = 7;   // one check per hour = one reminder per hour

async function externalHeartbeat(env) {
  if (!env.CHANNEL_LOGS || !env.DISCORD_BOT_TOKEN) {
    console.log("external heartbeat skipped: CHANNEL_LOGS/DISCORD_BOT_TOKEN not set");
    return;
  }
  if (new Date().getUTCMinutes() !== HEARTBEAT_TICK_MINUTE) return;

  // status=success, never status=completed: GitHub counts a FAILED run as
  // completed, so asking for completed runs would treat "watch has failed every
  // tick for two days" as a healthy pulse. This is the same bug the alert.yml
  // heartbeat had, and it must not be reintroduced here.
  const runsUrl = String(env.GITHUB_DISPATCH_URL || "").replace(
    /\/dispatches$/, "/runs?status=success&per_page=1");
  if (!runsUrl.endsWith("/runs?status=success&per_page=1")) {
    console.error("external heartbeat skipped: GITHUB_DISPATCH_URL is not a dispatches url");
    return;
  }

  let last = null;
  try {
    const res = await fetch(runsUrl, {
      headers: {
        Authorization: `Bearer ${env.GITHUB_DISPATCH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "notify-watcher-worker",
      },
    });
    if (res.ok) {
      const body = await res.json();
      last = body?.workflow_runs?.[0]?.updated_at || null;
    } else {
      // A billing lockout answers 403 here; an outage answers 5xx. Either way
      // GitHub cannot tell us the watcher is alive, so treat it as stale and
      // alert — failing open is the entire point of this check.
      console.error(`external heartbeat: GitHub returned HTTP ${res.status}`);
    }
  } catch (err) {
    console.error(`external heartbeat: could not reach GitHub: ${err}`);
  }

  const ageMs = last ? Date.now() - new Date(last).getTime() : Infinity;
  if (Number.isFinite(ageMs) && ageMs < HEARTBEAT_STALE_HOURS * 3600 * 1000) return;

  const ageText = Number.isFinite(ageMs)
    ? `${Math.floor(ageMs / 3600000)}h ago`
    : "unknown (GitHub did not answer)";
  const repoUrl = String(env.STATE_BASE_URL || "https://github.com")
    .replace("raw.githubusercontent.com", "github.com")
    .replace(/\/[^/]+$/, "/actions");  // strip the branch segment
  await postToDiscord(env, {
    title: `watch has not succeeded in ${HEARTBEAT_STALE_HOURS}h+`,
    description:
      `Checked from the Cloudflare Worker, outside GitHub. Last successful watch run: ` +
      `${last || "unknown"} (${ageText}). Because this check does not run on GitHub Actions, ` +
      `it still fires when Actions itself is unusable — a billing/spending-limit lockout, a ` +
      `disabled workflow, or a GitHub outage. In that situation the in-repo alert workflow ` +
      `cannot report anything, so this is the only notification you will get.`,
    url: repoUrl,
    color: 15158332,
  });
}

// Same red-embed shape alert.yml posts, so both heartbeats read alike in the
// LOGS channel. Failures are logged, never thrown (see scheduled()).
async function postToDiscord(env, embed) {
  try {
    const res = await fetch(
      `https://discord.com/api/v10/channels/${env.CHANNEL_LOGS}/messages`,
      {
        method: "POST",
        headers: {
          Authorization: `Bot ${env.DISCORD_BOT_TOKEN}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ embeds: [embed] }),
      }
    );
    if (!res.ok) {
      console.error(`external heartbeat: Discord returned HTTP ${res.status}`);
    }
  } catch (err) {
    console.error(`external heartbeat: Discord post failed: ${err}`);
  }
}

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
    case "latest":
      return await cmdLatest(opts.topic, env);

    // --- ON-DEMAND run (Phase 3): trigger a GitHub Actions sweep --------
    case "run":
      return await cmdRun(opts.topic, env);
    case "research": // /research url:<link> -> summarize + post the article
      return await cmdResearch(opts.url, env);

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
    const pendingDigest = Array.isArray(state.digest_buffer) ? state.digest_buffer.length : 0;
    const remindLater = state.later ? Object.keys(state.later).length : 0;
    const showMore = state.more_requests ? Object.keys(state.more_requests).length : 0;
    const mutedCount = state.muted ? Object.keys(state.muted).length : 0;
    return reply(
      [
        `Last sweep: ${last.ok ?? "?"} ok, ${last.failed ?? "?"} failed (${last.ts ?? "unknown"}).`,
        `Pending: ${pendingDigest} digest, ${remindLater} remind-later, ${showMore} show-more.`,
        `Muted topics: ${mutedCount}.`,
      ].join("\n")
    );
  }

  // NOTE (Phase 0 trap): muted topics live in state.muted; the live control
  // cursor is state.discord_control.last_id, NOT state.control (that one is the
  // dormant legacy ntfy cursor). And state.follows is the artist/streamer
  // overlay, while a followed TOPIC would live under state.followed.
  const muted = state.muted?.[topic];
  const th = state.topic_health?.[topic] || {};
  const lines = [
    `Status for ${human(topic)}:`,
    muted ? `Muted until ${muted}.` : "Not muted.",
    th.last_ok ? `Last ran OK at ${th.last_ok}.` : "No recent successful run recorded.",
  ];
  if (th.last_error) {
    lines.push(`Last error: ${String(th.last_error).slice(0, 200)}${th.last_error_ts ? ` (${th.last_error_ts})` : ""}.`);
  }
  return reply(lines.join("\n"));
}

async function cmdLatest(topic, env) {
  const state = await fetchState(env);
  if (!state) return reply("Could not read the latest state right now.");
  const key = String(topic || "").trim().toLowerCase();
  const log = Array.isArray(state.event_log) ? state.event_log : [];
  let found = null;
  for (let i = log.length - 1; i >= 0; i--) {
    const e = log[i];
    if (e && String(e.topic).toLowerCase() === key && e.action === "push") {
      found = e;
      break;
    }
  }
  if (!found) return reply(`No saved latest item for ${human(key)} yet.`);
  const lines = [
    `Latest ${human(key)}: ${found.title || "(untitled)"}`,
    found.source ? `Source: ${found.source}` : "",
    found.ts ? `When: ${found.ts}` : "",
    found.url || "",
  ].filter(Boolean);
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

async function cmdRun(topic, env) {
  if (!env.GITHUB_TOKEN || !env.GITHUB_DISPATCH_URL) {
    return reply("On-demand run is not configured yet.");
  }
  const only = String(topic || "").trim().toLowerCase();
  if (!only) return reply("Tell me which topic to run, e.g. /run topic:movies.");

  let res;
  try {
    res = await fetch(env.GITHUB_DISPATCH_URL, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "notify-watcher-worker",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: "main", inputs: { only } }),
    });
  } catch {
    return reply("Could not reach GitHub to start the run.");
  }

  if (res.status === 204) {
    return reply(`Started an on-demand check for ${human(only)}. A fresh result will post shortly if anything changed.`);
  }
  return reply(`Could not start the run (GitHub returned ${res.status}).`);
}

// /research url:<link> — same dispatch pattern as cmdRun, but the input is
// research_url: the workflow's research topic summarizes the article and
// posts it. The Worker stays a courier: it never fetches the article itself.
async function cmdResearch(url, env) {
  if (!env.GITHUB_TOKEN || !env.GITHUB_DISPATCH_URL) {
    return reply("On-demand research is not configured yet.");
  }
  const link = String(url || "").trim();
  if (!link) return reply("Give me a link to summarize, e.g. /research url:https://example.com/story.");
  // Reject non-URL input HERE, before dispatching: the workflow never runs for
  // junk, and the Python side never has to build a Discord embed around an
  // invalid click_url (Discord rejects those embeds with a 400).
  if (!isHttpUrl(link)) {
    return reply(
      "That doesn't look like a URL. Give me a full http(s) link, " +
      "e.g. /research url:https://example.com/story."
    );
  }

  let res;
  try {
    res = await fetch(env.GITHUB_DISPATCH_URL, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "notify-watcher-worker",
        "Content-Type": "application/json",
      },
      // only:"research" keeps the dispatch to the one on-demand topic instead
      // of dragging a full sweep along with every /research.
      body: JSON.stringify({ ref: "main", inputs: { research_url: link, only: "research" } }),
    });
  } catch {
    return reply("Could not reach GitHub to start the run.");
  }

  if (res.status === 204) {
    return reply("On it. The summary will post to Discord in a few minutes.");
  }
  return reply(`Could not start the run (GitHub returned ${res.status}).`);
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
// True only for a well-formed absolute http(s) URL. new URL() alone is not
// enough: it happily parses javascript:, ftp:, mailto: — none of which the
// research topic can fetch or Discord can embed as a click link.
function isHttpUrl(raw) {
  let parsed;
  try {
    parsed = new URL(raw);
  } catch {
    return false;
  }
  return parsed.protocol === "http:" || parsed.protocol === "https:";
}

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
 
