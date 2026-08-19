#!/usr/bin/env bash
# Build Hermes Foundry: edge, gateway, Hermes, and the Microsoft 365 sidecar in one image.
# The repository is self-contained; builds must use only source under ./src.
set -euo pipefail
cd "$(dirname "$0")"

: "${REG:?Set REG to the Azure Container Registry name (without .azurecr.io)}"
# A positional tag takes precedence over TAG. Agent versions are pinned to the resulting digest.
TAG="${1:-${TAG:-v1}}"

SRC=./src
[ -d "$SRC/edge" ] && [ -f "$SRC/gateway/app.py" ] && [ -f "$SRC/sidecar/agent365_mcp.py" ] || {
  echo "ERROR: $SRC is incomplete — expected src/edge, src/gateway/app.py, src/sidecar/agent365_mcp.py"; exit 1; }

# Fail if executable lines reach outside this repository.
if grep -vE '^[[:space:]]*#' "$0" | grep -qE '(^|[^.])\.\./'; then
  echo "ERROR: build.sh references a path outside the repository; builds must use only ./src:"
  grep -nE '(^|[^.])\.\./' "$0" | grep -vE '^[0-9]+:[[:space:]]*#'
  exit 1
fi

export DOTNET_ROOT="${DOTNET_ROOT:-$HOME/.dotnet}"
export PATH="$DOTNET_ROOT:$PATH"

echo "==> publishing the edge ($SRC/edge)"
rm -rf edge && mkdir -p edge
dotnet publish "$SRC/edge" -c Release -o ./edge
[ -f edge/HelloWorldA365Agent.dll ] || { echo "publish produced no HelloWorldA365Agent.dll"; exit 1; }

echo "==> staging gateway + sidecar"
mkdir -p gateway sidecar
cp "$SRC/gateway/app.py" gateway/
cp "$SRC/sidecar/agent365_mcp.py" sidecar/

echo "==> building in ACR (tag $TAG)"
az acr build --registry "$REG" --image "hermes-full-foundry:$TAG" --file Dockerfile .
echo "==> done: $REG.azurecr.io/hermes-full-foundry:$TAG"
