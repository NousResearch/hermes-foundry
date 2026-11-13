#!/usr/bin/env bash
# Submit an existing Foundry hosted agent as a private, tenant-scoped Microsoft 365 Autopilot
# blueprint. This does not submit anything to Marketplace, AppSource, or a public catalog.
#
# This is a DEVELOPER operation. It creates only a pending publication request. It does not accept
# Frontier terms, assign licenses, grant Microsoft Graph consent, choose a hiring scope, activate
# the blueprint, create an instance, or assign Azure roles to child Agent Identities.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ "${HERMES_DEPLOY_SKIP_LOCAL_CONFIG:-false}" != "true" && -f .deployment.local ]]; then
  source ./.deployment.local
fi

usage() {
  cat <<'EOF'
usage: publish-autopilot-blueprint.sh [--dry-run]

Required environment:
  ACCOUNT       Existing Microsoft Foundry account name
  PROJECT       Existing Foundry project name
  AGENT         Existing hosted-agent technical name

Optional:
  DISPLAY_NAME  Microsoft 365 display name; default "Hermes Agent on Foundry"
  APP_VERSION   Publication version; default "1.0.0"
  COLOR_ICON    192x192 PNG; default deployment/agent365/color.png
  OUTLINE_ICON  32x32 PNG; default deployment/agent365/outline.png

This command submits a tenant-scoped Autopilot publication request for administrator review. It
performs no tenant-administrator approval or instance-provisioning action.
EOF
}

DRY_RUN=false
case "${1:-}" in
  --dry-run) DRY_RUN=true ;;
  -h|--help) usage; exit 0 ;;
  "") ;;
  *) usage >&2; exit 2 ;;
esac

: "${ACCOUNT:?Set ACCOUNT}"
: "${PROJECT:?Set PROJECT}"
: "${AGENT:?Set AGENT}"
DISPLAY_NAME="${DISPLAY_NAME:-Hermes Agent on Foundry}"
APP_VERSION="${APP_VERSION:-1.0.0}"

[[ "$ACCOUNT" =~ ^[A-Za-z0-9][A-Za-z0-9-]{1,62}[A-Za-z0-9]$ ]] || {
  echo "ERROR: unsafe ACCOUNT name" >&2; exit 2;
}
[[ "$PROJECT" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{1,126}[A-Za-z0-9]$ ]] || {
  echo "ERROR: unsafe PROJECT name" >&2; exit 2;
}
[[ "$AGENT" =~ ^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$ ]] || {
  echo "ERROR: unsafe AGENT name" >&2; exit 2;
}
[[ "$APP_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo "ERROR: APP_VERSION must use major.minor.patch" >&2; exit 2;
}
if (( ${#DISPLAY_NAME} < 1 || ${#DISPLAY_NAME} > 30 )); then
  echo "ERROR: DISPLAY_NAME must be 1-30 characters for Microsoft Teams" >&2
  exit 2
fi

BASE="https://${ACCOUNT}.services.ai.azure.com/api/projects/${PROJECT}"
RESOURCE="https://ai.azure.com"
WORK=$(mktemp -d /tmp/hermes-autopilot-publish.XXXXXX)
cleanup() {
  case "$WORK" in
    /tmp/hermes-autopilot-publish.*) find "$WORK" -depth -delete 2>/dev/null || true ;;
  esac
}
trap cleanup EXIT

az rest --method GET --resource "$RESOURCE" \
  --url "${BASE}/agents/${AGENT}?api-version=v1" -o json > "$WORK/agent.json"

BLUEPRINT_ID=$(python3 - "$WORK/agent.json" <<'PY'
import json, sys
j = json.load(open(sys.argv[1]))
print((j.get("blueprint") or {}).get("client_id") or "")
PY
)
[[ "$BLUEPRINT_ID" =~ ^[0-9a-fA-F-]{36}$ ]] || {
  echo "ERROR: the hosted agent has no usable Entra Agent Identity Blueprint" >&2; exit 1;
}

python3 - "$WORK/agent.json" <<'PY'
import json, sys
j = json.load(open(sys.argv[1]))
endpoint = j.get("agent_endpoint") or {}
protocols = set(endpoint.get("protocols") or [])
schemes = {x.get("type") for x in endpoint.get("authorization_schemes") or []}
if "activity" not in protocols:
    raise SystemExit("ERROR: hosted agent must expose the activity protocol")
if "BotServiceTenant" not in schemes:
    raise SystemExit("ERROR: tenant publication requires BotServiceTenant authorization")
rules = ((endpoint.get("version_selector") or {}).get("version_selection_rules") or [])
if not any(x.get("traffic_percentage") == 100 for x in rules):
    raise SystemExit("ERROR: hosted agent has no 100% serving version")
PY

python3 - "$DISPLAY_NAME" "$APP_VERSION" "$BLUEPRINT_ID" "${COLOR_ICON:-deployment/agent365/color.png}" "${OUTLINE_ICON:-deployment/agent365/outline.png}" > "$WORK/request.json" <<'PY'
import base64, json, struct, sys
from pathlib import Path
display, version, blueprint, color, outline = sys.argv[1:]
def icon(path, size):
    data = Path(path).read_bytes()
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or struct.unpack(">II", data[16:24]) != (size, size):
        raise SystemExit(f"ERROR: expected {size}x{size} PNG icon: {path}")
    return base64.b64encode(data).decode("ascii")
json.dump({
    "agentDisplayName": display,
    "colorIconBase64": icon(color, 192),
    "outlineIconBase64": icon(outline, 32),
    "publishAsAutopilot": True,
    "publishScope": "Tenant",
    "appVersion": version,
    "canRespondWithoutMention": True,
    "shortDescription": "A Hermes AI colleague with its own Microsoft 365 identity.",
    "fullDescription": (
        "Hermes Agent on Foundry is an AI colleague hosted in Microsoft Foundry. Each instance "
        "receives its own Microsoft 365 identity and separately governed access. It can research "
        "the web, work with email and calendars, create and edit documents, share files, and run "
        "approved scheduled routines under its own auditable identity."
    ),
    "developerName": "Nous Research",
    "developerWebsiteUrl": "https://nousresearch.com",
    "privacyUrl": "https://nousresearch.com/privacy",
    "termsOfUseUrl": "https://nousresearch.com/terms",
    "useAgenticUserTemplate": True,
    "agenticUserTemplate": {
        "Id": "digitalWorkerTemplate",
        "File": "agenticUserTemplateManifest.json",
        "SchemaVersion": "0.1.0-preview",
        "AgentIdentityBlueprintId": blueprint,
        "CommunicationProtocol": "activityProtocol",
    },
}, sys.stdout)
PY

echo "Hosted agent: $AGENT"
echo "Microsoft 365 name: $DISPLAY_NAME"
echo "Publication version: $APP_VERSION"
echo "Publication scope: Tenant (administrator approval required)"
echo "Tenant consent, activation, instance creation, and RBAC performed: none"
if $DRY_RUN; then
  echo "DRY RUN complete. No publication request was submitted."
  exit 0
fi

TOKEN=$(az account get-access-token --resource "$RESOURCE" --query accessToken -o tsv)
PUBLICATION_ACTION=$(python3 scripts/publication_checkpoint.py begin "$BASE" "$AGENT" "$BLUEPRINT_ID" "$APP_VERSION")
if [[ "$PUBLICATION_ACTION" == "complete" ]]; then
  echo "Publication already completed for this version; resuming remaining setup."
  exit 0
fi

set +e
HTTP_STATUS=$(curl --silent --show-error \
  --connect-timeout 15 \
  --max-time 300 \
  --output "$WORK/response.json" \
  --write-out '%{http_code}' \
  --request POST \
  "${BASE}/agents/${AGENT}/microsoft365/publish?api-version=2025-11-15-preview" \
  --header "Authorization: Bearer ${TOKEN}" \
  --header "Content-Type: application/json" \
  --data-binary "@$WORK/request.json")
CURL_STATUS=$?
set -e

if [[ "$CURL_STATUS" -ne 0 ]]; then
  echo "ERROR: the Foundry publication request had a transport failure, so its outcome may be unknown." >&2
  echo "Check the Microsoft 365 pending-agent requests before rerunning this publication version." >&2
  exit 1
fi
[[ "$HTTP_STATUS" =~ ^[0-9]{3}$ ]] || {
  echo "ERROR: Foundry publication returned no valid HTTP status; do not assume it succeeded." >&2
  exit 1
}

if [[ "$HTTP_STATUS" != 2* ]]; then
  if python3 - "$WORK/response.json" <<'PY'
import json, sys
try:
    payload = json.load(open(sys.argv[1]))
except (OSError, json.JSONDecodeError):
    raise SystemExit(1)
error = payload.get("error") if isinstance(payload, dict) else None
code = str((error or {}).get("code") or "") if isinstance(error, dict) else ""
message = str((error or {}).get("message") or "") if isinstance(error, dict) else ""
raise SystemExit(0 if code == "UserError" and "version already exists" in message.lower() else 1)
PY
  then
    python3 scripts/publication_checkpoint.py complete "$BASE" "$AGENT" "$BLUEPRINT_ID" "$APP_VERSION" >/dev/null
    echo "Publication version ${APP_VERSION} already exists; the existing request was left unchanged."
    exit 0
  fi
  if [[ "$HTTP_STATUS" == 4* && "$HTTP_STATUS" != 408 ]]; then
    python3 scripts/publication_checkpoint.py rejected "$BASE" "$AGENT" "$BLUEPRINT_ID" "$APP_VERSION" >/dev/null
  fi
  echo "ERROR: Foundry publication returned HTTP ${HTTP_STATUS}." >&2
  echo "The response body was withheld because it can contain tenant and request identifiers." >&2
  exit 1
fi

python3 - "$WORK/response.json" <<'PY'
import json, sys
j = json.load(open(sys.argv[1]))
print("Developer publication completed.")
print("A tenant administrator must now review the pending Hermes Agent on Foundry request.")
for key in ("status", "state"):
    if j.get(key) is not None:
        print(f"Foundry {key}: {j[key]}")
PY

python3 scripts/publication_checkpoint.py complete "$BASE" "$AGENT" "$BLUEPRINT_ID" "$APP_VERSION" >/dev/null
