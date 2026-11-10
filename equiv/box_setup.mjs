#!/usr/bin/env node
// Upload helper scripts and start/stop/inspect the remote equivalent worker.
// Credentials from the environment. Never logs secret values or the SDK object.

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

function commandText(run) {
  if (!run) return "";
  for (const v of [run.result, run.stdout, run.output, run._result, run._stdout]) {
    if (typeof v === "string") return v;
  }
  return "";
}

function commandCode(run) {
  if (!run) return 0;
  const v = run.exitCode ?? run.code ?? run._exitCode;
  return typeof v === "number" ? v : 0;
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
  return { text: commandText(run), code: commandCode(run) };
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
  for (const name of ["dgram_stdio.py", "echo_remote.py", "oracle.py", "worker.py", "kill_leftovers.py"]) {
    const content = readFileSync(join(here, name), "utf8");
    await box.files.write({ path: `${remoteDir}/${name}`, content });
    console.log("WROTE", name, content.length);
  }
  const check = await exec(
    `python3 -m py_compile ${remoteDir}/dgram_stdio.py ${remoteDir}/echo_remote.py ${remoteDir}/oracle.py ${remoteDir}/worker.py ${remoteDir}/kill_leftovers.py && echo COMPILE_OK`
  );
  console.log(check.text.trim());
  if (!check.text.includes("COMPILE_OK")) process.exit(1);
} else if (cmd === "start-worker") {
  const workMs = process.env.WORK_MS || "120";
  const name = process.env.BOX_WORKER_NAME || "upstash-box";
  const listen = process.env.VEIL_AGENT_PORT || "27710";
  const controller = process.env.BOX_RELAY_PORT || "17810";
  // Free leftover workers via argv matching (not pkill), then nohup so the
  // worker can outlive a closed exec.session. Do not kill dgram_stdio here;
  // a live bridge session may be using it.
  const script = `cd ${remoteDir} && python3 kill_leftovers.py worker && sleep 0.3 && rm -f worker.pid worker.log && nohup python3 -u worker.py --listen 127.0.0.1:${listen} --controller 127.0.0.1:${controller} --name ${name} --work-ms ${workMs} > worker.log 2>&1 < /dev/null & echo $! > worker.pid && sleep 0.3 && if [ -d /proc/$(cat worker.pid) ]; then echo WORKER_STARTED; cat worker.pid; else echo WORKER_FAILED; tail -n 40 worker.log || true; exit 1; fi`;
  const r = await exec(script);
  console.log(r.text.trim());
  if (!r.text.includes("WORKER_STARTED")) process.exit(1);
} else if (cmd === "stop-worker") {
  const script = `cd ${remoteDir} && python3 kill_leftovers.py worker; rm -f worker.pid; echo STOPPED`;
  const r = await exec(script);
  console.log(r.text.trim());
} else if (cmd === "cleanup") {
  // Pre-run cleanup: workers and leftover relays. Not used mid-job.
  const st = statusName(await box.getStatus());
  console.log("BOX_STATUS", st);
  if (st === "paused") {
    await box.resume();
    console.log("RESUMED");
  }
  await exec(`mkdir -p ${remoteDir}`);
  const content = readFileSync(join(here, "kill_leftovers.py"), "utf8");
  await box.files.write({ path: `${remoteDir}/kill_leftovers.py`, content });
  const r = await exec(`cd ${remoteDir} && python3 kill_leftovers.py all; rm -f worker.pid; echo CLEANED`);
  console.log(r.text.trim());
  const inspect = await exec(
    `echo '--- proc ---'; python3 -c "import pathlib; t=('worker.py','dgram_stdio.py','echo_remote.py');
[print((p/'cmdline').read_bytes().replace(b'\\x00', b' ').decode(errors='replace')) for p in pathlib.Path('/proc').iterdir() if p.name.isdigit() and any((p/'cmdline').read_bytes().decode(errors='replace').find(n)>=0 for n in t)]" 2>/dev/null || true
echo '--- listen ---'
ss -ulnp 2>/dev/null | grep -E '27710|17810|18700|28701' || echo PORTS_FREE`
  );
  console.log(inspect.text.trim() || "(no inspect text)");
} else if (cmd === "inspect") {
  const script = `cd ${remoteDir}
echo '--- pid-file ---'
cat worker.pid 2>/dev/null || echo none
echo '--- proc ---'
found=
for pid in $(ls -d /proc/[0-9]* | sed 's@/proc/@@'); do
  cmd=$(tr '\\0' ' ' < /proc/$pid/cmdline 2>/dev/null || true)
  case "$cmd" in
    *-c*) continue ;;
    *worker.py*) found=$pid; echo ALIVE; echo "$cmd"; break ;;
  esac
done
[ -n "$found" ] || echo DEAD
echo '--- listen ---'
ss -ulnp 2>/dev/null | grep -E '27710|17810|18700' || netstat -ulnp 2>/dev/null | grep -E '27710|17810|18700' || true
echo '--- log-tail ---'
tail -n 20 worker.log 2>/dev/null || true`;
  const r = await exec(script);
  const out = process.env.INSPECT_OUT;
  if (out) writeFileSync(out, r.text);
  console.log(r.text);
} else {
  console.error("usage: box_setup.mjs resume|upload|start-worker|stop-worker|cleanup|inspect");
  process.exit(2);
}
