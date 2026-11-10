# runner-probe

A short, honest GitHub Actions runner benchmark for the owner’s own
distributed-compute project. It measures what a free `ubuntu-latest`
hosted runner looks like, how many of them actually run at once, and
whether one runner plus one already-provisioned Upstash box can share a
small job over the same outbound WebSocket datagram bridge used in the
lab.

This is **not** a general-purpose compute farm, a miner, or a keep-alive
service. Jobs are short by design. Do not add long-running loops, mining,
or work unrelated to this measurement.

GitHub’s own limits still apply: a person may have one free-plan machine
account; Actions is meant for the repository’s software lifecycle, not
general compute; and free accounts have concurrency and minute quotas.
A new account may also be asked to verify, or may have Actions disabled.
If any of those walls appear, stop and record the exact message rather
than trying to work around it.

## Workload used by the bridge test

The bridge workflow uses a **labeled equivalent workload**, not real
Veil.

Veil is a private source tree and must not be committed to this public
repository. The equivalent job reuses the lab’s outbound-only transport
(`equiv/dgram_stdio.py` and `equiv/upstash_bridge.mjs`, talking to an
Upstash box through the official `@upstash/box` `exec.session` API) and
the same prime-counting intervals the lab used:

- Comparison job: 40 intervals of 250,000 integers covering 0..10,000,000,
  with a 0.2 cross-check when two workers are present, scored against an
  independent sieve (the lab’s first interval count is 22,044; the total
  is 664,579).
- Mid-run job: the 80-task set from the lab reliability report (twenty
  1,000,000-wide windows in 50,000,000..70,000,000, each repeated four
  times).

A raw Python sieve of this range finishes in tens of milliseconds, which
would not show overlap or leave time to observe a mid-run worker loss.
Each task therefore also runs a short, bounded SHA-256 loop (`WORK_MS`,
default 120 ms for the comparison jobs and 200 ms for the mid-run job).
That loop is a timer, not mining: it is capped, uses a fixed input, and
does not search for a hash threshold.

If a future run needs the real Veil binary on the runner, do not put the
source or binary in this public repo. Pass a private artifact through
Actions secrets (`VEIL_BIN_URL`, `VEIL_BIN_SHA256`, `VEIL_WASM_URL`,
`VEIL_WASM_SHA256`) and extend the bridge job to download, verify, and
run it. The default path does not do that.

## What each workflow does

### `probe.yml`

Triggered only by `workflow_dispatch`. Input `jobs` is the number of
`ubuntu-latest` matrix legs (default 10, maximum 20). Input
`dispatch_time` should be the UTC ISO-8601 timestamp from the trigger so
queue time can be measured. A small setup job runs first to build the
exact matrix; that wait is included in each job’s queue time.

Each job writes `probe.json` with:

- matrix index and runner name
- dispatch time, job start time, and queue time
- `nproc`, `free -h`, `df -h /`, and the CPU model from `/proc/cpuinfo`
- a 20-second SHA-256 loop and hashes per second
- outbound reachability: HTTPS to `api.github.com` and the Upstash box
  API host, DNS, and a short outbound UDP note (STUN and DNS-over-UDP)
- the end time

Each job uploads its JSON. A final job writes `summary.json`, including
maximum concurrency computed from overlapping start and end times.

Expected runtime is well under two minutes per job (most of that is the
20-second hash loop). The hard cap is three minutes.

### `bridge.yml`

One `ubuntu-latest` job, hard-capped at five minutes, also
`workflow_dispatch` only. The runner is the outbound WebSocket client.
Secrets come only from Actions secrets: `UPSTASH_BOX_API_KEY`,
`UPSTASH_BOX_ID`, and optionally `UPSTASH_BOX_BASE_URL`. A paused box is
resumed without passing `keepAlive`.

The job measures:

1. Round-trip time from the runner to the box at 64 bytes and 60 KiB,
   20 samples each, with a byte-for-byte echo check (same adapter path
   as the lab).
2. The equivalent comparison job, run runner-only, box-only, and
   combined (runner worker + box worker, controller on the runner).
3. A correctness check against the independent sieve.
4. Runner-ends-mid-run: once both workers have finished at least one
   mid-run task, the runner-side worker is SIGKILL’d; the controller and
   box side are observed, including reassignment and time to finish.
   Then the outbound session is closed as if the Actions job had ended,
   the box is inspected, and recovery time is measured after the bridge
   (and the box worker, if it died) is restarted.

The remote worker is started with `nohup` so it can outlive a closed
`exec.session`, matching what would be interesting when a runner job
stops. The job always attempts to stop the remote worker afterwards.

## How to run

Do not run these workflows as a tight loop. One `jobs=10` probe, one
`jobs=20` probe, and one bridge run are enough for a measurement.

From the machine that holds the bot GitHub token and the Upstash env
file (never print either file):

```bash
# TOKEN_FILE must contain a PAT with repo and workflow scopes.
# UPSTASH_SECRETS_FILE is an env file with BOX_ID and BOX_API_KEY
# (run.sh maps those onto UPSTASH_BOX_ID and UPSTASH_BOX_API_KEY).
UPSTASH_SECRETS_FILE=/path/to/upstash.env \
  ./run.sh /path/to/gh.token veilgrok/runner-probe
```

`run.sh` will:

1. Create the public repository if it does not exist, and push this
   template.
2. Set Actions secrets with GitHub’s libsodium sealed-box API.
3. Dispatch `probe.yml` with `jobs=10`, wait, download artifacts.
4. Dispatch `probe.yml` with `jobs=20`, wait, download artifacts.
5. Dispatch `bridge.yml` once, wait, download artifacts.

Results land in `/workspace/veil-lab/gh-actions/results/` by default.
The script itself is safe to run only with a token file that belongs to
the bot account for this experiment. It will not touch
`BenItBuhner/Veil`.

You can also run a workflow from the Actions tab, passing `jobs` and a
`dispatch_time`.

## Limits

- Short jobs only. No mining, no unpaid keep-alive, no tight retries.
- Public repository: never put secrets, tokens, or private Veil source
  in the tree or in artifacts.
- Free-account Actions concurrency and minute quotas may cap how many
  `ubuntu-latest` jobs really overlap. The probe exists to measure that.
- The Upstash box must already exist. This repo does not create, pay
  for, or enable `keepAlive` on a box.
- Outbound UDP notes in the probe are observational only.

## Overflow pools (CircleCI, Buildkite, Blacksmith, Daytona, E2B)

These are off by default. A push to `main` must not start probe or bridge
jobs on any of them. Trigger scripts require `--confirm` and will not
run against a service until its token file exists and you opt in.

The Upstash dispatcher in `equiv/dispatcher.py` treats `gha`, `circleci`,
`buildkite`, `blacksmith`, `daytona`, `e2b`, and `upstash_box` as pluggable
pools. It issues per-task leases, renews them on worker heartbeats,
reassigns a task if a worker goes silent (the same mid-run-kill idea as
`bridge.yml`), and sends `drain` so workers exit themselves when the queue
is empty. Remote CI pools refuse to spawn unless `allow_remote=True`, and
even then this harness does not auto-trigger CircleCI, Buildkite or
Blacksmith. Daytona and E2B have API-driven pull adapters in
`equiv/pools.py`: the control plane creates sandboxes and execs each task
through the provider API. Pass `allow_remote=True` only when the key file
is present; tests use `fake=True` and never call the APIs.

Local test, simulated pools only (no CI, no Upstash, no tokens):

```bash
python3 equiv/test_dispatcher.py
```

### CircleCI

Published Free-plan limits used in `.circleci/config.yml`: docker
`medium` (2 vCPU / 4 GB) for the N-way concurrency matrix; docker
`large` (4 vCPU / 8 GB, the largest free x86 Docker class); docker
`arm.large` (4 vCPU / 16 GB, the largest free Arm Docker class); and
machine `arm.medium` (2 vCPU / 8 GB). Docker xlarge is Performance-only
and is not used. Workflows run only when the pipeline is triggered with
`run_probe=true` or `run_bridge=true`.

After the CircleCI project is connected to this GitHub repo (OAuth as
veilgrok / `bennettbuhner@mail.grokbot.com`):

1. Create a personal API token in CircleCI User Settings and write it to
   `/home/box/agent-data/secrets/circleci-veilgrok.token` (mode 600).
2. For the bridge job, set project environment variables
   `UPSTASH_BOX_API_KEY` and `UPSTASH_BOX_ID` (and optionally
   `UPSTASH_BOX_BASE_URL`) in the CircleCI UI. Do not put them in the repo.
3. Trigger a probe or the bridge:

```bash
python3 circleci_trigger.py --dry-run --jobs 10
python3 circleci_trigger.py --confirm --jobs 10
python3 circleci_trigger.py --confirm --bridge
```

The script polls the v2 API, writes job timings (queue and run seconds)
under `results/circleci-<pipeline-number>/timings.json`, and downloads
artifacts when they exist. It never prints the token.

### Buildkite

Published Free-plan limits: 2,000 Linux AMD64 hosted-agent vCPU minutes
per month, Small shape only (2 vCPU / 4 GB), 10 concurrent jobs / 20
vCPU. ARM and larger hosted shapes are not on the Free plan.

UI steps after signup:

1. Create a cluster (or use the default) and a hosted queue whose key is
   `hosted` with instance shape `LINUX_AMD64_2X4`.
2. Create a pipeline for this repo, YAML steps at
   `.buildkite/pipeline.yml`, in that same cluster. Turn off automatic
   branch / pull-request builds so a push does not start an empty build.
3. Write an API token (read/write builds) to
   `/home/box/agent-data/secrets/buildkite-veilgrok.token` (mode 600).
4. For the bridge job, set `UPSTASH_BOX_API_KEY` and `UPSTASH_BOX_ID` as
   pipeline environment variables.

```bash
python3 buildkite_trigger.py --dry-run --jobs 10
python3 buildkite_trigger.py --confirm --jobs 10
python3 buildkite_trigger.py --confirm --bridge
```

To spend no hosted minutes, start a short-lived self-hosted agent that
disconnects when idle, then trigger with `--queue ephemeral`:

```bash
BUILDKITE_AGENT_TOKEN_FILE=/home/box/agent-data/secrets/buildkite-veilgrok.agent.token \
  ./buildkite_ephemeral_agent.sh
```

The agent token is not the API token. Neither file is printed.

### Blacksmith

Published free allowance: 3,000 x64 2-vCPU minutes per month per GitHub
organization. 4 vCPU draws 2x; ARM 2-vCPU draws 0.625x. Blacksmith
requires a GitHub organization; a personal `veilgrok/runner-probe` repo
may need a free org before the app can be installed.

`.github/workflows/blacksmith-probe.yml` is `workflow_dispatch` only.
It runs N jobs on `blacksmith-2vcpu-ubuntu-2404`, plus one
`blacksmith-4vcpu-ubuntu-2404` and one `blacksmith-2vcpu-ubuntu-2404-arm`
when `include_larger` is true (the default). Do not dispatch it until
the Blacksmith GitHub App is installed; a run started earlier will sit
on "Waiting for a runner".

Once the app is installed (and only then):

```bash
# TOKEN_FILE is the existing veilgrok PAT. Never print it.
DISPATCH_TIME=$(python3 -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))')
GH_TOKEN=$(tr -d '\r\n' < /home/box/agent-data/secrets/gh-veilgrok.token) \
  gh workflow run blacksmith-probe.yml --repo veilgrok/runner-probe \
    -f jobs=10 -f dispatch_time="$DISPATCH_TIME"
```

### Daytona

The veilgrok Personal org is on Tier 1 ($100 free credit, no card). Tier 1
egress is restricted: GitHub and PyPI are reachable, the Upstash box host
is not, so this pool is always API-pull, never an outbound WebSocket.
Default cap is 5 sandboxes of 2 vCPU / 2 GiB (10 vCPU / 10 GiB, the
measured Tier 1 pool). Set `DAYTONA_API_KEY` or
`DAYTONA_API_KEY_FILE` (never print it). `DaytonaPool(..., allow_remote=True)`
creates ephemeral sandboxes, uploads `equiv/daytona_task.py`, and starts
local pull workers. Teardown deletes every sandbox it created.

### E2B

The veilgrok Hobby account has a $100 one-time credit. The measured default
template is 2 vCPU / 0.5 GiB, and 10 of them run at once (20 vCPU / 5 GiB).
Hobby sandboxes can reach the Upstash box host, so a WebSocket bridge from
inside a sandbox works; the dispatcher path is still API-pull so it stays
uniform with Daytona. Set `E2B_API_KEY` or `E2B_API_KEY_FILE`.
`E2BPool(..., allow_remote=True)` creates sandboxes and starts local pull
workers. Teardown kills every sandbox it created.

### Tokens and UI leftovers

| Pool | Token / secret file | Extra UI or app step |
| --- | --- | --- |
| GitHub Actions | `/home/box/agent-data/secrets/gh-veilgrok.token` (already present) | Actions secrets for the bridge (set by `run.sh`) |
| CircleCI | `/home/box/agent-data/secrets/circleci-veilgrok.token` | Connect the GitHub project; project env vars for the bridge |
| Buildkite | `/home/box/agent-data/secrets/buildkite-veilgrok.token` | Hosted queue `hosted` (`LINUX_AMD64_2X4`); pipeline YAML path; disable auto-builds; optional agent token for the ephemeral script |
| Blacksmith | same GitHub PAT | Install the Blacksmith GitHub App; may need a GitHub org |
| Daytona | `/home/box/agent-data/secrets/daytona-veilgrok.key` | None (email-only account already created). API-pull only on Tier 1 |
| E2B | `/home/box/agent-data/secrets/e2b-veilgrok.key` | None (Hobby, no card). API-pull dispatcher; optional in-sandbox WS to Box B |
| Upstash box | existing box env file | Already provisioned; do not enable `keepAlive` |
