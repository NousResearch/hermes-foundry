#!/usr/bin/env bash
# Install the tenant-side automation that grants Foundry User to future Agent Identities created
# from one Hermes Agent Identity Blueprint. This is deliberately an administrator bootstrap script,
# not an azd hook and not code executed by Hermes itself.
set -euo pipefail

cd "$(dirname "$0")/.."

usage() {
  cat <<'EOF'
Usage:
  ./scripts/install-agent-rbac-automation.sh \
    --resource-group <resource-group> \
    --account <foundry-account> \
    --project <foundry-project> \
    --blueprint-client-id <agent-identity-blueprint-app-id> \
    [--workflow-name <name>] [--location <azure-region>] [--poll-minutes <1-60>] \
    [--include-existing]

The signed-in administrator must be allowed to:
  * deploy a Consumption Logic App in the resource group;
  * grant Microsoft Graph AgentIdentity.Read.All application permission; and
  * create a constrained Azure RBAC role assignment at the Foundry project scope.

By default, identities that existed before installation are not changed. --include-existing makes
the first run reconcile every existing child identity as well.
EOF
}

RESOURCE_GROUP=""
ACCOUNT=""
PROJECT=""
BLUEPRINT_CLIENT_ID=""
WORKFLOW_NAME="hermes-agent-foundry-role-sync"
LOCATION=""
POLL_MINUTES="5"
INCLUDE_EXISTING="false"

while (($#)); do
  case "$1" in
    --resource-group) RESOURCE_GROUP="${2:?}"; shift 2 ;;
    --account) ACCOUNT="${2:?}"; shift 2 ;;
    --project) PROJECT="${2:?}"; shift 2 ;;
    --blueprint-client-id) BLUEPRINT_CLIENT_ID="${2:?}"; shift 2 ;;
    --workflow-name) WORKFLOW_NAME="${2:?}"; shift 2 ;;
    --location) LOCATION="${2:?}"; shift 2 ;;
    --poll-minutes) POLL_MINUTES="${2:?}"; shift 2 ;;
    --include-existing) INCLUDE_EXISTING="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

for value_name in RESOURCE_GROUP ACCOUNT PROJECT BLUEPRINT_CLIENT_ID; do
  if [[ -z "${!value_name}" ]]; then
    echo "Missing required argument: ${value_name}" >&2
    usage >&2
    exit 2
  fi
done

[[ "$POLL_MINUTES" =~ ^[0-9]+$ ]] && ((POLL_MINUTES >= 1 && POLL_MINUTES <= 60)) || {
  echo "--poll-minutes must be an integer from 1 to 60" >&2
  exit 2
}

command -v az >/dev/null || { echo "Azure CLI (az) is required" >&2; exit 2; }
command -v python3 >/dev/null || { echo "Python 3 is required" >&2; exit 2; }
az account show >/dev/null

if [[ "$(az provider show --namespace Microsoft.Logic --query registrationState -o tsv)" != "Registered" ]]; then
  echo "Registering the Microsoft.Logic resource provider for this subscription..."
  az provider register --namespace Microsoft.Logic --wait
fi

SUBSCRIPTION_ID=$(az account show --query id -o tsv)
PROJECT_SCOPE="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${RESOURCE_GROUP}/providers/Microsoft.CognitiveServices/accounts/${ACCOUNT}/projects/${PROJECT}"
WORKFLOW_ID="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${RESOURCE_GROUP}/providers/Microsoft.Logic/workflows/${WORKFLOW_NAME}"
FOUNDRY_USER_ROLE_ID="53ca6127-db72-4b80-b1b0-d745d6d5456d"
RBAC_ADMIN_ROLE_ID="f58310d9-a9f6-439a-9e8d-f62e7b41a168"
GRAPH_APP_ID="00000003-0000-0000-c000-000000000000"
GRAPH_AGENT_READ_ROLE_VALUE="AgentIdentity.Read.All"

az resource show --ids "$PROJECT_SCOPE" >/dev/null

if [[ -z "$LOCATION" ]]; then
  LOCATION=$(az group show --name "$RESOURCE_GROUP" --query location -o tsv)
fi

if [[ "$INCLUDE_EXISTING" == "true" ]]; then
  NOT_BEFORE="1970-01-01T00:00:00Z"
elif EXISTING_NOT_BEFORE=$(az resource show --ids "$WORKFLOW_ID" \
  --query properties.parameters.notBefore.value -o tsv 2>/dev/null) \
  && [[ -n "$EXISTING_NOT_BEFORE" ]]; then
  # Preserve the first installation boundary on an idempotent rerun. Moving it to "now" could
  # silently skip an identity created while permissions were still propagating.
  NOT_BEFORE="$EXISTING_NOT_BEFORE"
else
  NOT_BEFORE=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
fi

echo "Deploying $WORKFLOW_NAME in $RESOURCE_GROUP (poll interval: ${POLL_MINUTES}m)"
echo "Only Agent Identities under blueprint $BLUEPRINT_CLIENT_ID are in scope."
echo "Identity cutoff: $NOT_BEFORE"

DEPLOYMENT_NAME="${WORKFLOW_NAME}-$(date -u +%Y%m%d%H%M%S)"
DEPLOYMENT=$(az deployment group create \
  --name "$DEPLOYMENT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --template-file infra/agent-rbac-automation.json \
  --parameters \
    workflowName="$WORKFLOW_NAME" \
    location="$LOCATION" \
    blueprintClientId="$BLUEPRINT_CLIENT_ID" \
    projectScope="$PROJECT_SCOPE" \
    foundryUserRoleId="$FOUNDRY_USER_ROLE_ID" \
    notBefore="$NOT_BEFORE" \
    pollMinutes="$POLL_MINUTES" \
  -o json)

WORKFLOW_PRINCIPAL_ID=$(python3 -c \
  'import json,sys; print(json.load(sys.stdin)["properties"]["outputs"]["workflowPrincipalId"]["value"])' \
  <<<"$DEPLOYMENT")
[[ "$WORKFLOW_PRINCIPAL_ID" =~ ^[0-9a-fA-F-]{36}$ ]] || {
  echo "Deployment did not return a valid Logic App managed-identity principal ID" >&2
  exit 1
}

echo "Waiting for the Logic App managed identity to become visible in Entra..."
for _ in {1..30}; do
  if az ad sp show --id "$WORKFLOW_PRINCIPAL_ID" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
az ad sp show --id "$WORKFLOW_PRINCIPAL_ID" >/dev/null

GRAPH_SP_ID=$(az ad sp show --id "$GRAPH_APP_ID" --query id -o tsv)
GRAPH_AGENT_READ_ROLE_ID=$(az ad sp show --id "$GRAPH_APP_ID" \
  --query "appRoles[?value=='${GRAPH_AGENT_READ_ROLE_VALUE}' && contains(allowedMemberTypes, 'Application')].id | [0]" \
  -o tsv)
[[ "$GRAPH_AGENT_READ_ROLE_ID" =~ ^[0-9a-fA-F-]{36}$ ]] || {
  echo "Microsoft Graph does not expose the expected ${GRAPH_AGENT_READ_ROLE_VALUE} app role" >&2
  exit 1
}

EXISTING_GRAPH_GRANT=$(az rest --method GET \
  --url "https://graph.microsoft.com/v1.0/servicePrincipals/${WORKFLOW_PRINCIPAL_ID}/appRoleAssignments" \
  --query "value[?resourceId=='${GRAPH_SP_ID}' && appRoleId=='${GRAPH_AGENT_READ_ROLE_ID}'].id | [0]" \
  -o tsv)
if [[ -z "$EXISTING_GRAPH_GRANT" ]]; then
  echo "Granting the automation read-only Agent Identity discovery..."
  az rest --method POST \
    --url "https://graph.microsoft.com/v1.0/servicePrincipals/${WORKFLOW_PRINCIPAL_ID}/appRoleAssignments" \
    --headers Content-Type=application/json \
    --body "$(python3 -c \
      'import json,sys; print(json.dumps(dict(zip(("principalId","resourceId","appRoleId"),sys.argv[1:]))))' \
      "$WORKFLOW_PRINCIPAL_ID" "$GRAPH_SP_ID" "$GRAPH_AGENT_READ_ROLE_ID")" \
    --output none
else
  echo "Graph ${GRAPH_AGENT_READ_ROLE_VALUE} grant already exists."
fi

# This condition prevents the automation from assigning Owner, Contributor, or any other role. It
# can create or remove only Foundry User assignments, and only for service-principal identities.
CONDITION="((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})) OR (@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {${FOUNDRY_USER_ROLE_ID}} AND @Request[Microsoft.Authorization/roleAssignments:PrincipalType] ForAnyOfAnyValues:StringEqualsIgnoreCase {'ServicePrincipal'})) AND ((!(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'})) OR (@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {${FOUNDRY_USER_ROLE_ID}} AND @Resource[Microsoft.Authorization/roleAssignments:PrincipalType] ForAnyOfAnyValues:StringEqualsIgnoreCase {'ServicePrincipal'}))"

EXISTING_RBAC_DELEGATION=$(az role assignment list \
  --assignee-object-id "$WORKFLOW_PRINCIPAL_ID" \
  --scope "$PROJECT_SCOPE" \
  --query "[?roleDefinitionId=='/subscriptions/${SUBSCRIPTION_ID}/providers/Microsoft.Authorization/roleDefinitions/${RBAC_ADMIN_ROLE_ID}'].id | [0]" \
  -o tsv)
if [[ -z "$EXISTING_RBAC_DELEGATION" ]]; then
  echo "Granting constrained role-assignment delegation at the Foundry project..."
  az role assignment create \
    --assignee-object-id "$WORKFLOW_PRINCIPAL_ID" \
    --assignee-principal-type ServicePrincipal \
    --role "$RBAC_ADMIN_ROLE_ID" \
    --scope "$PROJECT_SCOPE" \
    --condition "$CONDITION" \
    --condition-version 2.0 \
    --description "May assign only Foundry User to service principals discovered under the configured Hermes blueprint." \
    --output none
else
  EXISTING_RBAC_CONDITION=$(az role assignment list \
    --assignee-object-id "$WORKFLOW_PRINCIPAL_ID" \
    --scope "$PROJECT_SCOPE" \
    --query "[?id=='${EXISTING_RBAC_DELEGATION}'].condition | [0]" \
    -o tsv)
  if [[ "$EXISTING_RBAC_CONDITION" != "$CONDITION" ]]; then
    echo "ABORT: the existing RBAC delegation is not constrained exactly as expected." >&2
    echo "Review $EXISTING_RBAC_DELEGATION in Azure Portal; this installer will not broaden or replace it." >&2
    exit 1
  fi
  echo "Constrained project RBAC delegation already exists."
fi

echo
echo "Installed successfully. New child Agent Identities are reconciled within about ${POLL_MINUTES} minutes."
echo "Logic App: https://portal.azure.com/#resource${PROJECT_SCOPE%/providers/Microsoft.CognitiveServices/accounts/*}/providers/Microsoft.Logic/workflows/${WORKFLOW_NAME}"
echo "Managed identity object ID: $WORKFLOW_PRINCIPAL_ID"
echo "Existing Agent Identities were $([[ "$INCLUDE_EXISTING" == "true" ]] && echo included || echo excluded)."
echo "A first run can report Graph 403 while the new application permission propagates; later runs retry automatically."
