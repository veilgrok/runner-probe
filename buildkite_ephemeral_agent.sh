#!/usr/bin/env bash
# Start a short-lived Buildkite agent in Docker (or the official binary)
# so a pipeline can run without hosted-agent minutes. The agent disconnects
# after it is idle, and this script does not leave it running.
#
# This is a self-hosted agent token, which is different from the API token
# used by buildkite_trigger.py. Never print either token. Do not run with
# set -x.
#
# Usage:
#   BUILDKITE_AGENT_TOKEN_FILE=/home/box/agent-data/secrets/buildkite-veilgrok.agent.token \
#     ./buildkite_ephemeral_agent.sh
#
# Optional env:
#   BUILDKITE_QUEUE          default: ephemeral
#   DISCONNECT_AFTER_IDLE    default: 60 (seconds)
#   AGENT_IMAGE              default: buildkite/agent:3
#   WORKDIR                  default: this repository
#
# Then trigger a build with:
#   python3 buildkite_trigger.py --queue ephemeral --confirm
set +x
set -euo pipefail
trap 'set +x' DEBUG
export PS4='+# buildkite_ephemeral_agent.sh suppressed #\n'

TOKEN_FILE=${BUILDKITE_AGENT_TOKEN_FILE:-/home/box/agent-data/secrets/buildkite-veilgrok.agent.token}
QUEUE=${BUILDKITE_QUEUE:-ephemeral}
IDLE=${DISCONNECT_AFTER_IDLE:-60}
IMAGE=${AGENT_IMAGE:-buildkite/agent:3}
WORKDIR=${WORKDIR:-$(cd "$(dirname "$0")" && pwd)}

if [[ ! -f "$TOKEN_FILE" ]]; then
  echo "agent token file not found at $TOKEN_FILE" >&2
  echo "Create an agent token in the Buildkite cluster's agent tokens page and write it there (mode 600)." >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is not available; install it or run buildkite-agent on the host." >&2
  exit 1
fi

# Token is passed as an env file so it does not appear on the docker command line.
ENV_FILE=$(mktemp)
chmod 600 "$ENV_FILE"
cleanup() {
  rm -f "$ENV_FILE"
}
trap cleanup EXIT
{
  printf 'BUILDKITE_AGENT_TOKEN='
  tr -d '\r\n' < "$TOKEN_FILE"
  printf '\nBUILDKITE_AGENT_NAME=ephemeral-%s\n' "$$"
} > "$ENV_FILE"

echo "Starting ephemeral Buildkite agent on queue '$QUEUE' (idle disconnect ${IDLE}s)."
echo "This uses a short-lived container and will exit when the agent is idle."
docker run --rm \
  --env-file "$ENV_FILE" \
  -v "$WORKDIR":/workspace:ro \
  -w /workspace \
  "$IMAGE" start \
  --tags "queue=${QUEUE}" \
  --disconnect-after-idle-timeout "$IDLE"
