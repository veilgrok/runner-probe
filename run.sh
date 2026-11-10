#!/usr/bin/env bash
# Create/push the public repo, set Actions secrets via libsodium sealed-box,
# dispatch probe (10, then 20) and bridge, download artifacts.
# Never prints token or secret values. Never run with set -x.
set +x
set -euo pipefail
# Refuse to turn xtrace back on.
trap 'set +x' DEBUG
export PS4='+# run.sh suppressed #\n'

usage() {
  echo "Usage: UPSTASH_SECRETS_FILE=/path/to/upstash.env $0 TOKEN_FILE OWNER/REPO" >&2
  echo "  TOKEN_FILE              GitHub PAT file (repo + workflow scopes). Never printed." >&2
  echo "  OWNER/REPO              e.g. veilgrok/runner-probe" >&2
  echo "  UPSTASH_SECRETS_FILE    env file with BOX_ID and BOX_API_KEY (required to set secrets / run bridge)" >&2
  echo "  RESULTS_DIR             default /workspace/veil-lab/gh-actions/results" >&2
  echo "  PROBE_JOBS_FIRST        default 10" >&2
  echo "  PROBE_JOBS_SECOND       default 20" >&2
  echo "  SKIP_PROBE              set to 1 to skip probe dispatches" >&2
  echo "  SKIP_BRIDGE             set to 1 to skip the bridge dispatch" >&2
  echo "  SKIP_PUSH               set to 1 to dispatch only (repo already pushed)" >&2
  echo "  POLL_SECONDS            default 15" >&2
  echo "  PROBE_TIMEOUT_SECONDS   default 1800" >&2
  echo "  BRIDGE_TIMEOUT_SECONDS  default 900" >&2
  exit 2
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || "${#}" -lt 2 ]]; then
  usage
fi

TOKEN_FILE=$1
REPO=$2
TEMPLATE_DIR=$(cd "$(dirname "$0")" && pwd)
RESULTS_DIR=${RESULTS_DIR:-/workspace/veil-lab/gh-actions/results}
PROBE_JOBS_FIRST=${PROBE_JOBS_FIRST:-10}
PROBE_JOBS_SECOND=${PROBE_JOBS_SECOND:-20}
SKIP_PROBE=${SKIP_PROBE:-0}
SKIP_BRIDGE=${SKIP_BRIDGE:-0}
SKIP_PUSH=${SKIP_PUSH:-0}
POLL_SECONDS=${POLL_SECONDS:-15}
PROBE_TIMEOUT_SECONDS=${PROBE_TIMEOUT_SECONDS:-1800}
BRIDGE_TIMEOUT_SECONDS=${BRIDGE_TIMEOUT_SECONDS:-900}
UPSTASH_SECRETS_FILE=${UPSTASH_SECRETS_FILE:-}

if [[ ! -f "$TOKEN_FILE" ]]; then
  echo "token file not found" >&2
  exit 1
fi
if [[ "$REPO" != */* ]]; then
  echo "repo must be OWNER/REPO" >&2
  exit 1
fi
OWNER=${REPO%%/*}
NAME=${REPO#*/}

# Load the token into gh's environment only. Do not echo it, and do not put it
# on a command line that tools might log.
GH_TOKEN=$(tr -d '\r\n' < "$TOKEN_FILE")
export GH_TOKEN
export GH_HOST=github.com
export GIT_TERMINAL_PROMPT=0

api() {
  # Thin wrapper so error bodies (which should not contain the token) are shown.
  local out rc
  set +e
  out=$(gh api "$@" 2>&1)
  rc=$?
  set -e
  if [[ $rc -ne 0 ]]; then
    echo "gh api failed (exit $rc) for: gh api $*" >&2
    echo "$out" >&2
    return $rc
  fi
  printf '%s' "$out"
}

echo "Authenticated as: $(gh api user --jq .login)"

if [[ "$SKIP_PUSH" != 1 ]]; then
  cd "$TEMPLATE_DIR"
  if [[ -e secrets.env || -n "$(find . -name '*.token' -o -name '*.key' | grep -v './.git/' || true)" ]]; then
    echo "refusing to push: token/key/secrets.env file present in the template tree" >&2
    find . -name '*.token' -o -name '*.key' -o -name 'secrets.env' >&2 || true
    exit 1
  fi
  if [[ ! -d .git ]]; then
    git init -b main
    git config user.name "$OWNER"
    git config user.email "bennettbuhner@mail.grokbot.com"
  fi
  git add -A
  if git status --porcelain | grep -E '\.token$|\.key$|secrets\.env$' >/dev/null; then
    echo "refusing to commit a secret-looking path" >&2
    git status --porcelain >&2
    exit 1
  fi
  if ! git diff --cached --quiet || ! git diff --quiet; then
    git add -A
  fi
  if git diff --cached --quiet; then
    if ! git rev-parse --verify HEAD >/dev/null 2>&1; then
      echo "nothing to commit" >&2
      exit 1
    fi
    echo "No new changes to commit."
  else
    git commit -m "Add Actions runner probe and Upstash bridge measurement workflows."
  fi

  gh auth setup-git >/dev/null
  if gh repo view "$REPO" >/dev/null 2>&1; then
    echo "Repo $REPO already exists."
    if ! git remote get-url origin >/dev/null 2>&1; then
      git remote add origin "https://github.com/${REPO}.git"
    fi
  else
    echo "Creating public repo $REPO"
    # gh uses GH_TOKEN; the token is not placed in the remote URL.
    if ! gh repo create "$REPO" --public --description "Short GitHub Actions runner benchmark for a distributed-compute measurement." --source="$TEMPLATE_DIR" --remote=origin; then
      echo "gh repo create failed. Exact output is above. Stopping." >&2
      exit 1
    fi
  fi
  git push -u origin main
fi

# --- Set Actions secrets with the libsodium sealed-box API (PyNaCl) ---
if [[ -n "$UPSTASH_SECRETS_FILE" ]]; then
  if [[ ! -f "$UPSTASH_SECRETS_FILE" ]]; then
    echo "UPSTASH_SECRETS_FILE not found" >&2
    exit 1
  fi
  echo "Setting Actions secrets from the Upstash env file (values not printed)."
  python3 - "$REPO" "$UPSTASH_SECRETS_FILE" <<'PY'
import json, os, sys, subprocess, base64
from pathlib import Path
from nacl import public

repo, env_path = sys.argv[1], sys.argv[2]
values = {}
for line in Path(env_path).read_text().splitlines():
    s = line.strip()
    if not s or s.startswith("#") or "=" not in s:
        continue
    k, v = s.split("=", 1)
    values[k.strip()] = v.strip().strip("'").strip('"')

mapping = {
    "UPSTASH_BOX_ID": values.get("BOX_ID") or values.get("UPSTASH_BOX_ID"),
    "UPSTASH_BOX_API_KEY": values.get("BOX_API_KEY") or values.get("UPSTASH_BOX_API_KEY"),
}
if values.get("BOX_BASE_URL") or values.get("UPSTASH_BOX_BASE_URL"):
    mapping["UPSTASH_BOX_BASE_URL"] = values.get("BOX_BASE_URL") or values.get("UPSTASH_BOX_BASE_URL")

missing = [k for k, v in mapping.items() if not v]
if missing:
    print("missing keys for secrets:", ", ".join(missing), file=sys.stderr)
    sys.exit(1)

def gh_api(*args, input_text=None):
    cmd = ["gh", "api", *args]
    r = subprocess.run(cmd, input=input_text, text=True, capture_output=True)
    if r.returncode != 0:
        print("gh api failed:", " ".join(args), file=sys.stderr)
        print(r.stderr, file=sys.stderr)
        sys.exit(r.returncode)
    return r.stdout

pk = json.loads(gh_api(f"repos/{repo}/actions/secrets/public-key"))
key_id = pk["key_id"]
pub = public.PublicKey(base64.b64decode(pk["key"]))
sealed = public.SealedBox(pub)
for name, value in mapping.items():
    encrypted = base64.b64encode(sealed.encrypt(value.encode("utf-8"))).decode("ascii")
    body = json.dumps({"encrypted_value": encrypted, "key_id": key_id})
    gh_api(f"repos/{repo}/actions/secrets/{name}", "-X", "PUT", "--input", "-", input_text=body)
    print(f"set secret {name}")
PY
else
  echo "UPSTASH_SECRETS_FILE unset; not changing Actions secrets."
fi

iso_now() {
  python3 - <<'PY'
from datetime import datetime, timezone
print(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
PY
}

dispatch() {
  local workflow=$1
  local timeout=$2
  shift 2
  local when
  when=$(iso_now)
  echo "Dispatching $workflow at $when $*"
  # Build -f args
  local fields=(-f "dispatch_time=${when}")
  local arg
  for arg in "$@"; do
    fields+=(-f "$arg")
  done
  if ! gh workflow run "$workflow" --repo "$REPO" "${fields[@]}"; then
    echo "workflow dispatch failed for $workflow. Stopping." >&2
    exit 1
  fi
  # The dispatch API returns no run id. Poll for a new run created at or after 'when'.
  local deadline=$(( $(date +%s) + timeout ))
  local run_id=""
  sleep 3
  while [[ $(date +%s) -lt $deadline ]]; do
    run_id=$(gh run list --repo "$REPO" --workflow "$workflow" --json databaseId,createdAt,status,url,conclusion,event \
      --limit 10 --jq --arg when "$when" \
      '[.[] | select(.event=="workflow_dispatch" and .createdAt >= $when)] | .[0].databaseId // empty' 2>/dev/null || true)
    # gh --jq --arg may not work that way; fallback to python.
    if [[ -z "$run_id" ]]; then
      run_id=$(gh run list --repo "$REPO" --workflow "$workflow" --json databaseId,createdAt,status,url,conclusion,event --limit 10 \
        | python3 -c "import json,sys; when=sys.argv[1]; rows=json.load(sys.stdin); hits=[r for r in rows if r.get('event')=='workflow_dispatch' and r.get('createdAt','')>=when[:19]]; print(hits[0]['databaseId'] if hits else '')" "$when")
    fi
    if [[ -n "$run_id" ]]; then
      break
    fi
    sleep "$POLL_SECONDS"
  done
  if [[ -z "$run_id" ]]; then
    echo "No run appeared for $workflow after dispatch. Listing recent runs:" >&2
    gh run list --repo "$REPO" --workflow "$workflow" --limit 5 >&2 || true
    exit 1
  fi
  echo "Run id $run_id for $workflow"
  gh run view "$run_id" --repo "$REPO" --json url,status,conclusion --jq '{url,status,conclusion}'
  if ! gh run watch "$run_id" --repo "$REPO" --exit-status; then
    echo "Run $run_id did not succeed. Fetching view:" >&2
    gh run view "$run_id" --repo "$REPO" >&2 || true
    # Still try to download any artifacts, then exit non-zero after.
    local dest="$RESULTS_DIR/${workflow%.yml}-$run_id"
    mkdir -p "$dest"
    gh run download "$run_id" --repo "$REPO" -D "$dest" || true
    echo "Artifacts (if any) saved under $dest" >&2
    exit 1
  fi
  local dest="$RESULTS_DIR/${workflow%.yml}-$run_id"
  mkdir -p "$dest"
  gh run download "$run_id" --repo "$REPO" -D "$dest"
  echo "Downloaded artifacts to $dest"
  # Record the run URL next to the artifacts.
  gh run view "$run_id" --repo "$REPO" --json url,status,conclusion,displayTitle,createdAt,updatedAt \
    --jq '{url,status,conclusion,title:.displayTitle,createdAt,updatedAt}' \
    > "$dest/run-meta.json" || true
}

mkdir -p "$RESULTS_DIR"

if [[ "$SKIP_PROBE" != 1 ]]; then
  dispatch probe.yml "$PROBE_TIMEOUT_SECONDS" "jobs=${PROBE_JOBS_FIRST}"
  dispatch probe.yml "$PROBE_TIMEOUT_SECONDS" "jobs=${PROBE_JOBS_SECOND}"
fi
if [[ "$SKIP_BRIDGE" != 1 ]]; then
  dispatch bridge.yml "$BRIDGE_TIMEOUT_SECONDS"
fi

echo "All requested dispatches finished. Results in $RESULTS_DIR"
