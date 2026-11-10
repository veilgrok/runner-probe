#!/usr/bin/env node
// Adapter echo RTT: runner -> exec.session frames -> remote UDP echo -> back.
// Same measurement as the lab veil-echo.mts / adapter_echo.py path.
// Credentials from the environment. Never logs secret values.

import { randomBytes } from "node:crypto";
import { writeFileSync } from "node:fs";
import { Box } from "@upstash/box";

function required(name) {
  const v = process.env[name];
  if (!v) {
    console.error(`missing required env ${name}`);
    process.exit(1);
  }
  return v;
}

const id = required("UPSTASH_BOX_ID");
const apiKey = required("UPSTASH_BOX_API_KEY");
process.env.UPSTASH_BOX_API_KEY = apiKey;
const baseUrl = (process.env.UPSTASH_BOX_BASE_URL || "https://us-east-1.box.upstash.com").replace(/\/$/, "");
const remoteScript = process.env.BOX_ECHO_SCRIPT || "/home/boxuser/veil-lab/gha/echo_remote.py";
const outPath = process.env.RTT_OUT || "rtt.json";

const box = await Box.get(id, { apiKey, baseUrl });
const st = await box.getStatus();
const stName = typeof st === "string" ? st : (st && st.status) || "unknown";
console.log("BOX_STATUS", stName);
if (stName === "paused") {
  await box.resume();
  console.log("RESUMED");
}

let buf = Buffer.alloc(0);
let waiting = null;
const session = await box.exec.session({
  argv: ["python3", "-u", remoteScript],
  tty: false,
  onStdout: (data) => {
    buf = Buffer.concat([buf, Buffer.from(data)]);
    while (buf.length >= 2) {
      const n = buf.readUInt16BE(0);
      if (buf.length < 2 + n) break;
      const d = Buffer.from(buf.subarray(2, 2 + n));
      buf = buf.subarray(2 + n);
      if (waiting) {
        const w = waiting;
        waiting = null;
        clearTimeout(w.timer);
        w.resolve({ ms: performance.now() - w.start, correct: d.equals(w.payload), timeout: false });
      }
    }
  },
  onStderr: (d) => console.log("REMOTE", Buffer.from(d).toString().trim()),
});

// Wait briefly for the remote echo relay to print readiness on stderr.
await new Promise((r) => setTimeout(r, 400));

const series = [
  [64, 20],
  [60000, 20],
];
const rows = [];
for (const [size, count] of series) {
  const samples = [];
  for (let i = 0; i < count; i++) {
    const payload = randomBytes(size);
    const h = Buffer.alloc(2);
    h.writeUInt16BE(size);
    const result = await new Promise((resolve) => {
      waiting = {
        resolve,
        payload,
        start: performance.now(),
        timer: setTimeout(() => {
          waiting = null;
          resolve({ timeout: true, ms: null, correct: false });
        }, 5000),
      };
      session.write(Buffer.concat([h, payload]));
    });
    samples.push(result);
  }
  const vals = samples.filter((x) => !x.timeout).map((x) => x.ms).sort((a, b) => a - b);
  const mean = vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
  rows.push({
    size,
    count,
    ok: samples.filter((x) => x.correct).length,
    timeouts: samples.filter((x) => x.timeout).length,
    exact_echoes: `${samples.filter((x) => x.correct).length}/${count}`,
    min_ms: vals[0] ?? null,
    mean_ms: mean,
    p50_ms: vals.length ? vals[Math.floor(vals.length / 2)] : null,
    max_ms: vals.length ? vals[vals.length - 1] : null,
    first_sample_ms: samples[0].ms,
    samples,
  });
}

writeFileSync(outPath, JSON.stringify({ rows }, null, 2));
console.log("RTT_SUMMARY", JSON.stringify(rows.map(({ samples, ...r }) => r)));
try { session.close(); } catch {}
process.exit(rows.every((r) => r.timeouts === 0 && r.ok === r.count) ? 0 : 1);
