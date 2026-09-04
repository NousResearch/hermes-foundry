#!/usr/bin/env bash
# Remove the installed role-sync workflow and its delegation. This does not remove Foundry User
# assignments that the workflow already created; those remain explicit, auditable Azure resources.
set -euo pipefail

usage() {
  echo "Usage: $0 --resource-group <rg> --account <account> --project <project> [--workflow-name <name>]" >&2
}

RESOURCE_GROUP=""
ACCOUNT=""
PROJECT=""
WORKFLOW_NAME="hermes-agent-foundry-role-sync"
while (($#)); do
  case "$1" in
    --resource-group) RESOURCE_GROUP="${2:?}"; shift 2 ;;
    --account) ACCOUNT="${2:?}"; shift 2 ;;
    --project) PROJECT="${2:?}"; shift 2 ;;
    --workflow-name) WORKFLOW_NAME="${2:?}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

[[ -n "$RESOURCE_GROUP" && -n "$ACCOUNT" && -n "$PROJECT" ]] || { usage; exit 2; }

SUBSCRIPTION_ID=$(az account show --query id -o tsv)
PROJECT_SCOPE="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${RESOURCE_GROUP}/providers/Microsoft.CognitiveServices/accounts/${ACCOUNT}/projects/${PROJECT}"
WORKFLOW_ID="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${RESOURCE_GROUP}/providers/Microsoft.Logic/workflows/${WORKFLOW_NAME}"
RBAC_ADMIN_ROLE_ID="f58310d9-a9f6-439a-9e8d-f62e7b41a168"

PRINCIPAL_ID=$(az resource show --ids "$WORKFLOW_ID" --query identity.principalId -o tsv 2>/dev/null || true)
if [[ -n "$PRINCIPAL_ID" ]]; then
  while IFS= read -r assignment_id; do
    [[ -z "$assignment_id" ]] || az role assignment delete --ids "$assignment_id"
  done < <(az role assignment list \
    --assignee-object-id "$PRINCIPAL_ID" \
    --scope "$PROJECT_SCOPE" \
    --query "[?roleDefinitionId=='/subscriptions/${SUBSCRIPTION_ID}/providers/Microsoft.Authorization/roleDefinitions/${RBAC_ADMIN_ROLE_ID}'].id" \
    -o tsv)
fi

az resource delete --ids "$WORKFLOW_ID"
echo "Removed $WORKFLOW_NAME and its project-scoped delegation. Existing agent Foundry User assignments were left unchanged."
