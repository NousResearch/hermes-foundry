#!/usr/bin/env bash
# Plan or remove the tenant-side routine RBAC reconciler. The default is deliberately read-only.
# Destructive execution requires both --execute and the exact workflow name as confirmation.
set -euo pipefail

cd "$(dirname "$0")/.."

usage() {
  cat <<'EOF'
Usage:
  ./scripts/remove-agent-rbac-automation.sh \
    --subscription <subscription-id> \
    --resource-group <resource-group> \
    --account <foundry-account> \
    --project <foundry-project> \
    --blueprint-client-id <agent-identity-blueprint-app-id> \
    [--workflow-name <name>]

The default command is a read-only removal plan. To perform the verified removal, repeat it with:
    --execute --confirm-workflow-name <exact-workflow-name>

Removal deletes only the Logic App and its constrained project delegation. Foundry User role
assignments already created for Agent Identities are intentionally left unchanged.
EOF
}

SUBSCRIPTION_ID=""
RESOURCE_GROUP=""
ACCOUNT=""
PROJECT=""
BLUEPRINT_CLIENT_ID=""
WORKFLOW_NAME="hermes-agent-foundry-role-sync"
WORKFLOW_EXPLICIT=false
EXECUTE="false"
CONFIRM_WORKFLOW_NAME=""

while (($#)); do
  case "$1" in
    --subscription) SUBSCRIPTION_ID="${2:?}"; shift 2 ;;
    --resource-group) RESOURCE_GROUP="${2:?}"; shift 2 ;;
    --account) ACCOUNT="${2:?}"; shift 2 ;;
    --project) PROJECT="${2:?}"; shift 2 ;;
    --blueprint-client-id) BLUEPRINT_CLIENT_ID="${2:?}"; shift 2 ;;
    --workflow-name) WORKFLOW_NAME="${2:?}"; WORKFLOW_EXPLICIT=true; shift 2 ;;
    --execute) EXECUTE="true"; shift ;;
    --confirm-workflow-name) CONFIRM_WORKFLOW_NAME="${2:?}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ "$SUBSCRIPTION_ID" =~ ^[0-9A-Fa-f-]{36}$ ]] \
  || { printf 'ERROR: --subscription must be a subscription GUID.\n' >&2; exit 2; }
[[ "$BLUEPRINT_CLIENT_ID" =~ ^[0-9A-Fa-f-]{36}$ ]] \
  || { printf 'ERROR: --blueprint-client-id must be an application GUID.\n' >&2; exit 2; }
[[ "$RESOURCE_GROUP" =~ ^[A-Za-z0-9._()/-]{1,90}$ && "$RESOURCE_GROUP" != */* ]] \
  || { printf 'ERROR: unsafe or missing --resource-group.\n' >&2; exit 2; }
[[ "$ACCOUNT" =~ ^[A-Za-z0-9][A-Za-z0-9-]{1,62}[A-Za-z0-9]$ ]] \
  || { printf 'ERROR: unsafe or missing --account.\n' >&2; exit 2; }
[[ "$PROJECT" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{1,126}[A-Za-z0-9]$ ]] \
  || { printf 'ERROR: unsafe or missing --project.\n' >&2; exit 2; }
[[ "$WORKFLOW_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{1,78}[A-Za-z0-9]$ ]] \
  || { printf 'ERROR: unsafe --workflow-name.\n' >&2; exit 2; }
command -v az >/dev/null || { printf 'ERROR: Azure CLI (az) is required.\n' >&2; exit 2; }
command -v python3 >/dev/null || { printf 'ERROR: Python 3 is required.\n' >&2; exit 2; }

ACTIVE_SUBSCRIPTION=$(az account show --query id --output tsv)
ACTIVE_SUBSCRIPTION_LOWER=$(printf '%s' "$ACTIVE_SUBSCRIPTION" | tr '[:upper:]' '[:lower:]')
SUBSCRIPTION_ID_LOWER=$(printf '%s' "$SUBSCRIPTION_ID" | tr '[:upper:]' '[:lower:]')
if [[ "$ACTIVE_SUBSCRIPTION_LOWER" != "$SUBSCRIPTION_ID_LOWER" ]]; then
  printf 'ERROR: Azure CLI is using a different subscription. No changes were made.\n' >&2
  exit 2
fi

PROJECT_SCOPE="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${RESOURCE_GROUP}/providers/Microsoft.CognitiveServices/accounts/${ACCOUNT}/projects/${PROJECT}"
WORKFLOW_ID="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${RESOURCE_GROUP}/providers/Microsoft.Logic/workflows/${WORKFLOW_NAME}"
RBAC_ADMIN_ROLE_ID="f58310d9-a9f6-439a-9e8d-f62e7b41a168"
FOUNDRY_USER_ROLE_ID="53ca6127-db72-4b80-b1b0-d745d6d5456d"
WORK=$(mktemp -d /tmp/hermes-rbac-removal.XXXXXX)
cleanup() {
  case "$WORK" in
    /tmp/hermes-rbac-removal.*) find "$WORK" -depth -delete 2>/dev/null || true ;;
  esac
}
trap cleanup EXIT

# Prove that the requested Foundry target exists before considering the dependent workflow.
az resource show --ids "$PROJECT_SCOPE" --output none

if [[ "$WORKFLOW_EXPLICIT" != true ]]; then
  az resource list --resource-group "$RESOURCE_GROUP" \
    --resource-type Microsoft.Logic/workflows --output json > "$WORK/workflows.json"
  WORKFLOW_NAME=$(python3 scripts/select_rbac_workflow.py \
    --project-scope "$PROJECT_SCOPE" --blueprint-id "$BLUEPRINT_CLIENT_ID" \
    --inventory "$WORK/workflows.json")
  WORKFLOW_ID="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${RESOURCE_GROUP}/providers/Microsoft.Logic/workflows/${WORKFLOW_NAME}"
fi

if ! az resource show --ids "$WORKFLOW_ID" --output json > "$WORK/workflow.json"; then
  printf 'ERROR: the Logic App could not be read. It may be absent, inaccessible, or unavailable.\n' >&2
  printf 'No changes were made; verify its state in Azure before retrying.\n' >&2
  exit 1
fi

PRINCIPAL_ID=$(python3 - "$WORK/workflow.json" "$PROJECT_SCOPE" "$BLUEPRINT_CLIENT_ID" \
  "$FOUNDRY_USER_ROLE_ID" <<'PY'
import json, re, sys

workflow = json.load(open(sys.argv[1]))
parameters = (workflow.get("properties") or {}).get("parameters") or {}

def value(name):
    item = parameters.get(name) or {}
    return str(item.get("value") or "")

if value("projectScope").lower() != sys.argv[2].lower():
    raise SystemExit("ABORT: Logic App projectScope does not match the requested Foundry project")
if value("blueprintClientId").lower() != sys.argv[3].lower():
    raise SystemExit("ABORT: Logic App blueprintClientId does not match the requested blueprint")
if value("foundryUserRoleId").lower() != sys.argv[4].lower():
    raise SystemExit("ABORT: Logic App is not configured to assign only the Foundry User role")
principal = str((workflow.get("identity") or {}).get("principalId") or "")
if not re.fullmatch(r"[0-9a-fA-F-]{36}", principal):
    raise SystemExit("ABORT: Logic App has no valid managed-identity principal to verify")
print(principal)
PY
)

az role assignment list \
  --assignee-object-id "$PRINCIPAL_ID" \
  --scope "$PROJECT_SCOPE" \
  --query "[?roleDefinitionId=='/subscriptions/${SUBSCRIPTION_ID}/providers/Microsoft.Authorization/roleDefinitions/${RBAC_ADMIN_ROLE_ID}']" \
  --output json > "$WORK/assignments.json"

ASSIGNMENT_COUNT=$(python3 - "$WORK/assignments.json" <<'PY'
import json, sys
items = json.load(open(sys.argv[1]))
if not isinstance(items, list):
    raise SystemExit("ABORT: Azure returned an invalid role-assignment response")
print(len(items))
PY
)

printf 'Verified removal plan:\n'
printf '  Logic App: %s\n' "$WORKFLOW_NAME"
printf '  Foundry project target: verified\n'
printf '  Agent Identity blueprint target: verified\n'
printf '  Matching project-delegation assignments: %s\n' "$ASSIGNMENT_COUNT"
printf '  Existing Agent Identity Foundry User assignments: retained\n'

if [[ "$EXECUTE" != "true" ]]; then
  printf 'PLAN ONLY: no resources or permissions were changed.\n'
  printf 'To execute, repeat with --execute --confirm-workflow-name %s\n' "$WORKFLOW_NAME"
  exit 0
fi

if [[ "$CONFIRM_WORKFLOW_NAME" != "$WORKFLOW_NAME" ]]; then
  printf 'ABORT: --confirm-workflow-name must exactly match %s. No changes were made.\n' \
    "$WORKFLOW_NAME" >&2
  exit 2
fi

python3 - "$WORK/assignments.json" <<'PY' > "$WORK/assignment-ids.txt"
import json, sys
for item in json.load(open(sys.argv[1])):
    identifier = str(item.get("id") or "")
    if identifier:
        print(identifier)
PY

while IFS= read -r assignment_id; do
  [[ -z "$assignment_id" ]] || az role assignment delete --ids "$assignment_id"
done < "$WORK/assignment-ids.txt"

az resource delete --ids "$WORKFLOW_ID"
if az resource show --ids "$WORKFLOW_ID" --output none >/dev/null 2>&1; then
  printf 'ERROR: Azure still reports the Logic App after deletion. Verify state before retrying.\n' >&2
  exit 1
fi

printf 'Removed %s and its project-scoped delegation.\n' "$WORKFLOW_NAME"
printf 'Existing Agent Identity Foundry User assignments were left unchanged.\n'
