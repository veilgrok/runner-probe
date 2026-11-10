#!/usr/bin/env node
// Upload helper scripts and start/stop/inspect the remote equivalent worker.
// Credentials from the environment. Never logs secret values.

import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { Box } from "@upstash/box";

function required(name) {
  const v = process.env[name];
  if (!v) {
    console.error(`missing required env ${name}`);
    process.exit(1);
  }
  return v;
}

function statusName(st) {
  return typeof st === "string" ? st : (st && st.status) || "unknown";
}

const id = required("UPSTASH_BOX_ID");
const apiKey = required("UPSTASH_BOX_API_KEY");
process.env.UPSTASH_BOX_API_KEY = apiKey;
const baseUrl = (process.env.UPSTASH_BOX_BASE_URL || "https://us-east-1.box.upstash.com").replace(/\/$/, "");
const cmd = process.argv[2] || "help";
const remoteDir = process.env.BOX_GHA_DIR || "/home/boxuser/veil-lab/gha";
const here = dirname(fileURLToPath(import.meta.url));

const box = await Box.get(id, { apiKey, baseUrl });

async function exec(command) {
  const run = await box.exec.command(command);
  const result = run && (run.result ?? run.stdout ?? run.output ?? "");
  const text = typeof result === "string" ? result : JSON.stringify(result ?? "");
  const code = run && (run.exitCode ?? run.code ?? 0);
  return { text, code, run };
}

if (cmd === "resume") {
  const st = statusName(await box.getStatus());
  console.log("BOX_STATUS", st);
  if (st === "paused") {
    await box.resume();
    console.log("RESUMED");
  }
} else if (cmd === "upload") {
  const st = statusName(await box.getStatus());
  console.log("BOX_STATUS", st);
  if (st === "paused") {
    await box.resume();
    console.log("RESUMED");
  }
  await exec(`mkdir -p ${remoteDir}`);
  for (const name of ["dgram_stdio.py", "echo_remote.py", "oracle.py", "worker.py"]) {
    const content = readFileSync(join(here, name), "utf8");
    await box.files.write({ path: `${remoteDir}/${name}`, content });
    console.log("WROTE", name, content.length);
  }
  const check = await exec(`python3 -m py_compile ${remoteDir}/dgram_stdio.py ${remoteDir}/echo_remote.py ${remoteDir}/oracle.py ${remoteDir}/worker.py && echo COMPILE_OK`);
  console.log(check.text.trim());
  if (!check.text.includes("COMPILE_OK")) process.exit(1);
} else if (cmd === "start-worker") {
  const workMs = process.env.WORK_MS || "120";
  const name = process.env.BOX_WORKER_NAME || "upstash-box";
  const listen = process.env.VEIL_AGENT_PORT || "27710";
  const controller = process.env.BOX_RELAY_PORT || "17810";
  // Free the port from any leftover worker, then nohup so it can outlive a closed exec.session.
  const script = `cd ${remoteDir} && python3 -c "import os,signal,pathlib;
for p in pathlib.Path('/proc').iterdir():

    if not p.name.isdigit(): continue
    try: c=open(p/'cmdline','rb').read().replace(bytes([0]),b' ').decode();
    except Exception: continue
    if 'worker.py' in c and 'python' in c and int(p.name)!=os.getpid():
        os.kill(int(p.name), signal.SIGKILL); print('killed', p.name)" ; sleep 0.3; rm -f worker.pid worker.log && nohup python3 -u worker.py --listen 127.0.0.1:${listen} --controller 127.0.0.1:${controller} --name ${name} --work-ms ${workMs} > worker.log 2>&1 < /dev/null & echo $! > worker.pid && sleep 0.3 && if [ -d /proc/$(cat worker.pid) ]; then echo WORKER_STARTED; cat worker.pid; else echo WORKER_FAILED; tail -n 40 worker.log || true; exit 1; fi`;
  const r = await exec(script);
  console.log(r.text.trim());
  if (!r.text.includes("WORKER_STARTED")) process.exit(1);
} else if (cmd === "stop-worker") {
  const script = `python3 -c "import os,signal,pathlib;
for p in pathlib.Path('/proc').iterdir():
    if not p.name.isdigit(): continue
    try: c=open(p/'cmdline','rb').read().replace(bytes([0]),b' ').decode();
    except Exception: continue
    if 'worker.py' in c and 'python' in c and int(p.name)!=os.getpid():
        os.kill(int(p.name), signal.SIGKILL); print('killed', p.name)" ; rm -f ${remoteDir}/worker.pid; echo STOPPED`;
  const r = await exec(script);
  console.log(r.text.trim());
} else if (cmd === "inspect") {
  const script = `echo '--- pid ---'; cat ${remoteDir}/worker.pid 2>/dev/null || echo none; echo '--- proc ---'; if [ -f ${remoteDir}/worker.pid ] && [ -d /proc/$(cat ${remoteDir}/worker.pid) ]; then echo ALIVE; tr '\\0' ' ' < /proc/$(cat ${remoteDir}/worker.pid)/cmdline; echo; else echo DEAD; fi; echo '--- listen ---'; ss -ulnp 2>/dev/null | grep -E '27710|17810|18700' || netstat -ulnp 2>/dev/null | grep -E '27710|17810|18700' || true; echo '--- log-tail ---'; tail -n 20 ${remoteDir}/worker.log 2>/dev/null || true`;
  const r = await exec(script);
  const out = process.env.INSPECT_OUT;
  if (out) writeFileSync(out, r.text);
  console.log(r.text);
} else {
  console.error("usage: box_setup.mjs resume|upload|start-worker|stop-worker|inspect");
  process.exit(2);
}
