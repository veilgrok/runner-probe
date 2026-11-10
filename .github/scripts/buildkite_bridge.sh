#!/bin/bash
# Buildkite Upstash bridge job. Kept as a file so pipeline upload does not
# interpolate $vars in an inline command (which also hid the test exit code).
set +x
python3 - <<'PY'
import json, os, shutil
info = {
    "has_upstash_key": bool(os.environ.get("UPSTASH_BOX_API_KEY")),
    "has_upstash_id": bool(os.environ.get("UPSTASH_BOX_ID")),
    "has_upstash_base": bool(os.environ.get("UPSTASH_BOX_BASE_URL")),
    "npm": bool(shutil.which("npm")),
    "node": bool(shutil.which("node")),
    "python3": bool(shutil.which("python3")),
}
open("bridge-preflight.json", "w").write(json.dumps(info, indent=2) + "\n")
print(json.dumps(info, sort_keys=True))
PY
buildkite-agent annotate --style info --context bridge-preflight < bridge-preflight.json || true

missing=
[ -n "${UPSTASH_BOX_API_KEY:-}" ] || missing="$missing UPSTASH_BOX_API_KEY"
[ -n "${UPSTASH_BOX_ID:-}" ] || missing="$missing UPSTASH_BOX_ID"
if [ -n "$missing" ]; then
  echo "Missing required Buildkite environment variables:$missing" | tee bridge-error.txt
  buildkite-agent annotate --style error --context bridge-error < bridge-error.txt || true
  exit 1
fi
if [ "${UPSTASH_BOX_API_KEY}" = "[set]" ] || [ "${UPSTASH_BOX_ID}" = "[set]" ]; then
  echo "UPSTASH credentials look redacted ([set]); the trigger must send real env values, not redact_env() output." | tee bridge-error.txt
  buildkite-agent annotate --style error --context bridge-error < bridge-error.txt || true
  exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
  sudo apt-get update -y && sudo apt-get install -y nodejs npm
fi
npm install --omit=dev --no-audit --no-fund

set +e
python3 equiv/run_bridge_test.py
rc=$?
set -e

if [ -f bridge-out/bridge-summary.json ]; then
  buildkite-agent annotate --style success --context bridge-summary < bridge-out/bridge-summary.json || true
fi
if [ -f bridge-out/bridge-report.json ]; then
  buildkite-agent annotate --style info --context bridge-report < bridge-out/bridge-report.json || true
fi
if [ -f bridge-out/rtt.json ]; then
  buildkite-agent annotate --style info --context rtt < bridge-out/rtt.json || true
fi
if [ "$rc" -ne 0 ]; then
  {
    echo "run_bridge_test.py exited $rc"
    ls -la bridge-out 2>/dev/null || true
    # Never dump env or secrets. Include only sanitized log tails.
    for f in bridge-out/bridge.log bridge-out/controller.log bridge-out/worker.log; do
      if [ -f "$f" ]; then
        echo "---- $f (last 80 lines, redacted) ----"
        sed -E 's/([Aa][Pp][Ii][_-]?[Kk][Ee][Yy]|[Tt]oken|[Ss]ecret)[=:][^[:space:]]+/\1=[redacted]/g' "$f" | tail -n 80
      fi
    done
  } | tee -a bridge-error.txt
  buildkite-agent annotate --style error --context bridge-error < bridge-error.txt || true
  exit "$rc"
fi
