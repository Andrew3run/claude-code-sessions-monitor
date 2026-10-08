#!/usr/bin/env node
// Optional helper for Sessions Monitor: saves the plan usage limits that Claude Code passes to the
// status line command, so the monitor can show "session" and "week" usage and their reset time.
//
// Option A - you have no custom status line: use this file as it is. In ~/.claude/settings.json:
//   "statusLine": { "type": "command", "command": "node C:/path/to/statusline-usage.js" }
//
// Option B - you already have a status line script: copy the block marked below into it, right
// after you parse the JSON read from stdin (here called `data`). Do not run both.
//
// Only `rate_limits` is written, to ~/.claude/usage-snapshot.json. Nothing is sent anywhere.
const fs = require('fs');
const os = require('os');
const path = require('path');

let data = {};
try { data = JSON.parse(fs.readFileSync(0, 'utf8')); } catch {}

// ---- begin block to copy into an existing status line script ----
try {
  if (data.rate_limits) {
    fs.writeFileSync(
      path.join(os.homedir(), '.claude', 'usage-snapshot.json'),
      JSON.stringify({ at: Date.now(), rate_limits: data.rate_limits }));
  }
} catch {}
// ---- end block ----

// Option A prints a minimal status line: the model name.
process.stdout.write(data.model?.display_name || '');
