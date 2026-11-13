#!/usr/bin/env bash
# Build the Hermes Foundry image: Hermes + edge + gateway + M365 sidecar in one container.
#
# ⛔ PLAN F HAS ITS OWN SOURCE, IN ./src — IT MUST NOT REACH OUTSIDE THIS DIRECTORY.
#
# This build used to publish the edge from ../agent-identity/foundry-autopilot/src/hello_world_a365_agent
# and copy the gateway and sidecar from ../ as well. That made Plan F and the THIN Foundry stack
# (`.fndr`, declared stable and frozen — see STABLE_FNDR.md) share one set of source files.
#
# Why that was actively dangerous, from 2026-08-17: chasing an hfull-only symptom, a behavioural change
# was made to the shared edge — altering WHICH CALLERS THE AGENT WILL ANSWER — and the reasoning "I will
# only deploy it to hfull" was treated as sufficient. It is not: the frozen stack's next rebuild would
# have silently inherited it. The freeze cannot depend on remembering which files are shared.
#
# So ./src is a full, committed, independent copy. Changes here affect Plan F and nothing else, and the
# guard below fails the build if a `../` path ever creeps back in.
set -euo pipefail
cd "$(dirname "$0")"

# Optional, gitignored per-installation settings. Every tracked checkout remains tenant-neutral.
if [ "${HERMES_BUILD_SKIP_LOCAL_CONFIG:-false}" != "true" ] && [ -f .deployment.local ]; then
  source ./.deployment.local
fi
: "${REG:?Set REG to the Azure Container Registry name (or add it to .deployment.local)}"
# Positional first, env second. `build.sh v18` silently produced a `v1` image when TAG was env-only,
# and a mis-tagged image is genuinely dangerous here: the agent version pins a tag, so the wrong one
# deploys old code under a new version number and every conclusion drawn from it is wrong.
TAG="${1:-${TAG:-v1}}"

SRC=./src                      # the FORK — never a parent path (the guard below enforces it)
[ -d "$SRC/edge" ] && [ -f "$SRC/gateway/app.py" ] && [ -f "$SRC/sidecar/agent365_mcp.py" ] || {
  echo "ERROR: $SRC is incomplete — expected src/edge, src/gateway/app.py, src/sidecar/agent365_mcp.py"; exit 1; }

# ISOLATION GUARD. If this script ever references a parent path again, fail loudly rather than quietly
# re-coupling Plan F to the frozen thin-Foundry source.
# Strip comments BEFORE matching. An earlier version piped `grep -n` into `grep -v '^\s*#'`, which
# never excluded anything — grep -n prefixes the line number, so the line starts with a digit, not a
# hash. The guard then failed the build by flagging its own documentation.
if grep -vE '^[[:space:]]*#' "$0" | grep -qE '(^|[^.])\.\./'; then
  echo "ERROR: build.sh references a path outside full-foundry/ — Plan F must build only from ./src:"
  grep -nE '(^|[^.])\.\./' "$0" | grep -vE '^[0-9]+:[[:space:]]*#'
  exit 1
fi

echo "==> building in ACR (tag $TAG)"
# Dockerfile compiles the committed edge source in its own SDK stage and copies the committed
# gateway and sidecar directly. The operator therefore needs neither Docker nor .NET locally.
BUILD_CONTEXT=$(mktemp -d)
trap 'rm -rf "$BUILD_CONTEXT"' EXIT
python3 scripts/stage-build-context.py "$BUILD_CONTEXT"
az acr build --registry "$REG" --image "hermes-full-foundry:$TAG" --file Dockerfile "$BUILD_CONTEXT"
echo "==> done: $REG.azurecr.io/hermes-full-foundry:$TAG"
