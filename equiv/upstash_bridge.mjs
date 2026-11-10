#!/usr/bin/env node
// Outbound-only Upstash box.exec.session datagram bridge.
// Lab direction: controller and this client run on the GitHub Actions runner.
// Remote dgram_stdio.py on an already-provisioned Upstash box.
// Credentials come from the environment. Never logs secret values.

import { createSocket } from "node:dgram";
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
const localRelay = Number(process.env.VEIL_LOCAL_RELAY_PORT || 17710);
const agentPort = Number(process.env.VEIL_AGENT_PORT || 27710);
const boxRelay = Number(process.env.BOX_RELAY_PORT || 17810);
const controllerUdp = Number(process.env.VEIL_CONTROLLER_UDP || 7700);
const remoteScript = process.env.BOX_DGRAM_SCRIPT || "/home/boxuser/veil-lab/gha/dgram_stdio.py";

const box = await Box.get(id, { apiKey, baseUrl });
const st = await box.getStatus();
const stName = typeof st === "string" ? st : (st && st.status) || "unknown";
console.log("BOX_STATUS", stName);
if (stName === "paused") {
  // Resume without keepAlive, matching the existing lab helper.
  await box.resume();
  console.log("RESUMED");
}

const udp = createSocket("udp4");
await new Promise((resolve, reject) => {
  udp.once("error", reject);
  udp.bind(localRelay, "127.0.0.1", resolve);
});

let buf = Buffer.alloc(0);
let up = 0;
let down = 0;

const session = await box.exec.session({
  argv: ["python3", "-u", remoteScript, String(boxRelay), String(agentPort)],
  tty: false,
  onStdout: (data) => {
    buf = Buffer.concat([buf, Buffer.from(data)]);
    while (buf.length >= 2) {
      const n = buf.readUInt16BE(0);
      if (buf.length < 2 + n) break;
      const pkt = buf.subarray(2, 2 + n);
      buf = buf.subarray(2 + n);
      udp.send(pkt, controllerUdp, "127.0.0.1");
      up += 1;
    }
  },
  onStderr: (d) => console.log("REMOTE", Buffer.from(d).toString().trim()),
});

udp.on("message", (d) => {
  const h = Buffer.alloc(2);
  h.writeUInt16BE(d.length);
  session.write(Buffer.concat([h, d]));
  down += 1;
});

console.log(
  "BRIDGE_READY",
  "local_relay",
  localRelay,
  "agent",
  agentPort,
  "box_relay",
  boxRelay,
  "controller",
  controllerUdp
);
const timer = setInterval(() => console.log("BRIDGE_PACKETS", "up", up, "down", down), 10000);

function shutdown() {
  try { session.close(); } catch {}
  try { udp.close(); } catch {}
  clearInterval(timer);
  process.exit(0);
}
process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);

const rc = await session.wait();
console.log("BRIDGE_EXIT", rc);
clearInterval(timer);
udp.close();
process.exit(rc === 0 ? 0 : 1);
