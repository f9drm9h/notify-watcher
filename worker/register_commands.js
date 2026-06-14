/**
 * register_commands.js — one-time registration of notify-watcher's Discord
 * slash commands as GLOBAL commands.
 *
 * Standalone utility: it does NOT import the Worker. It reads DISCORD_TOKEN and
 * DISCORD_APPLICATION_ID from the project-root .env, then PUTs the full command
 * set to https://discord.com/api/v10/applications/{id}/commands.
 *
 * A PUT (bulk overwrite) is idempotent: re-running it leaves exactly this set
 * registered, so it is safe to run again. Global commands can take up to an
 * hour to appear in clients, but the API response confirms registration.
 *
 * Run:  node worker/register_commands.js
 */

const fs = require("fs");
const path = require("path");

// Discord application command option types we use.
const STRING = 3;
const INTEGER = 4;

// ----- read credentials from the project-root .env ----------------------
function loadEnv(envPath) {
  if (!fs.existsSync(envPath)) {
    console.error(`No .env file found at ${envPath}`);
    process.exit(1);
  }
  const out = {};
  for (const rawLine of fs.readFileSync(envPath, "utf8").split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq === -1) continue;
    const key = line.slice(0, eq).trim();
    let val = line.slice(eq + 1).trim();
    // strip matching surrounding quotes if present
    if (
      (val.startsWith('"') && val.endsWith('"')) ||
      (val.startsWith("'") && val.endsWith("'"))
    ) {
      val = val.slice(1, -1);
    }
    out[key] = val;
  }
  return out;
}

const env = loadEnv(path.join(__dirname, "..", ".env"));
const TOKEN = env.DISCORD_TOKEN;
const APP_ID = env.DISCORD_APPLICATION_ID;

if (!TOKEN) {
  console.error("DISCORD_TOKEN is missing or empty in .env");
  process.exit(1);
}
if (!APP_ID) {
  console.error("DISCORD_APPLICATION_ID is missing or empty in .env");
  process.exit(1);
}

// ----- the command set --------------------------------------------------
// Note: Discord's command schema has no per-option "default value" field.
// The Worker supplies the 24h fallback itself (opts.hours ?? 24); here we
// only mark whether each option is required.
const commands = [
  {
    name: "ping",
    description: "Check that notify-watcher is responding.",
  },
  {
    name: "status",
    description: "Show the latest sweep status, optionally for one topic.",
    options: [
      {
        type: STRING,
        name: "topic",
        description: "Topic to report on (omit for the overall last sweep).",
        required: false,
      },
    ],
  },
  {
    name: "explain",
    description: "Explain recent routing decisions for a topic.",
    options: [
      {
        type: STRING,
        name: "topic",
        description: "Topic to explain.",
        required: true,
      },
    ],
  },
  {
    name: "mute",
    description: "Mute a topic for a number of hours (defaults to 24).",
    options: [
      {
        type: STRING,
        name: "topic",
        description: "Topic to mute.",
        required: true,
      },
      {
        type: INTEGER,
        name: "hours",
        description: "How many hours to mute for (omit for 24).",
        required: false,
      },
    ],
  },
  {
    name: "unmute",
    description: "Unmute a topic.",
    options: [
      {
        type: STRING,
        name: "topic",
        description: "Topic to unmute.",
        required: true,
      },
    ],
  },
  {
    name: "follow",
    description: "Follow a topic for a number of hours (defaults to 24).",
    options: [
      {
        type: STRING,
        name: "topic",
        description: "Topic to follow.",
        required: true,
      },
      {
        type: INTEGER,
        name: "hours",
        description: "How many hours to follow for (omit for 24).",
        required: false,
      },
    ],
  },
  {
    name: "unfollow",
    description: "Unfollow a topic.",
    options: [
      {
        type: STRING,
        name: "topic",
        description: "Topic to unfollow.",
        required: true,
      },
    ],
  },
  {
    name: "done",
    description: "Mark the current item done and skip the next nudge.",
  },
  {
    name: "more",
    description: "Ask for the fuller story on the next sweep.",
  },
  {
    name: "readlater",
    description: "Save the current item to your reading list.",
  },
];

// ----- register (bulk overwrite) ---------------------------------------
async function main() {
  const url = `https://discord.com/api/v10/applications/${APP_ID}/commands`;
  const res = await fetch(url, {
    method: "PUT",
    headers: {
      Authorization: `Bot ${TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(commands),
  });

  const text = await res.text();
  console.log(`HTTP ${res.status} ${res.statusText}`);

  let data;
  try {
    data = JSON.parse(text);
  } catch {
    console.log(text);
    process.exit(res.ok ? 0 : 1);
  }

  if (!res.ok) {
    console.error("Discord rejected the registration:");
    console.error(JSON.stringify(data, null, 2));
    process.exit(1);
  }

  const names = data.map((c) => c.name);
  console.log(`Discord confirmed ${names.length} registered command(s):`);
  for (const n of names) console.log(`  - ${n}`);
}

main().catch((err) => {
  console.error("Request failed:", err);
  process.exit(1);
});
