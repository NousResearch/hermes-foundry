#!/usr/bin/env bash
# Build Hermes, create its Foundry-managed identity blueprint, declare its Graph permissions, and
# submit a private tenant publication request. Microsoft 365 approval and instance creation remain
# explicit administrator/user operations after this script finishes.
set -euo pipefail

cd "$(dirname "$0")/.."

required=(ACCOUNT PROJECT AGENT REG TAG PRIMARY_MODEL FALLBACK_MODEL RESOURCE_GROUP LOCATION SUBSCRIPTION_ID TENANT_ID BLUEPRINT_ID MAIB_NAME BOT_SERVICE_ID)
for name in "${required[@]}"; do
  [[ -n "${!name:-}" ]] || {
    printf 'ERROR: azd did not supply required deployment output %s.\n' "$name" >&2
    exit 1
  }
done

export ACCOUNT PROJECT AGENT REG TAG PRIMARY_MODEL FALLBACK_MODEL BLUEPRINT_ID MAIB_NAME
az account set --subscription "$SUBSCRIPTION_ID"

printf '\nBuilding the Hermes image in Azure Container Registry...\n'
az acr build \
  --registry "$REG" \
  --image "hermes-full-foundry:${TAG}" \
  --file Dockerfile \
  .

base="https://${ACCOUNT}.services.ai.azure.com/api/projects/${PROJECT}"
resource="https://ai.azure.com"
output_dir="deployment-output/${AGENT}"
mkdir -p "$output_dir"

printf '\nWaiting for the deployment administrator\047s Foundry User role to become effective...\n'
foundry_access=false
for _ in $(seq 1 30); do
  if az rest --method GET --resource "$resource" \
    --url "${base}/deployments?api-version=v1" --output none >/dev/null 2>&1; then
    foundry_access=true
    break
  fi
  sleep 10
done
[[ "$foundry_access" == true ]] || {
  printf 'ERROR: Foundry User was assigned at project scope but did not become effective within five minutes. Rerun ./setup.sh to resume.\n' >&2
  exit 1
}

if az rest --method GET --resource "$resource" \
  --url "${base}/agents/${AGENT}?api-version=v1" \
  --output json > "${output_dir}/existing-agent.json" 2>/dev/null; then
  printf '\nThe hosted agent already exists; preserving it and resuming publication.\n'
  LIVE_BLUEPRINT_ID=$(python3 - "${output_dir}/existing-agent.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
print((data.get("blueprint") or {}).get("client_id") or "")
PY
  )
  [[ "$LIVE_BLUEPRINT_ID" =~ ^[0-9A-Fa-f-]{36}$ ]] || {
    printf 'ERROR: existing hosted agent has no usable blueprint client ID.\n' >&2
    exit 1
  }
  live_blueprint_lower=$(printf '%s' "$LIVE_BLUEPRINT_ID" | tr '[:upper:]' '[:lower:]')
  expected_blueprint_lower=$(printf '%s' "${BLUEPRINT_ID}" | tr '[:upper:]' '[:lower:]')
  [[ "$live_blueprint_lower" == "$expected_blueprint_lower" ]] || {
    printf 'ERROR: existing hosted agent is bound to a different identity blueprint.\n' >&2
    exit 1
  }
else
  printf '\nCreating the Hermes hosted agent bound to its explicit managed identity blueprint...\n'
  ./scripts/create-hosted-blueprint.sh
fi

printf '\nSubmitting Hermes Agent on Foundry for private tenant review...\n'
DISPLAY_NAME="Hermes Agent on Foundry" ./scripts/publish-autopilot-blueprint.sh

# Foundry publication has reset blueprint permission declarations in previous hfull deployments.
# Reconcile only after publishing so the tenant administrator reviews the final permission set.
printf '\nDeclaring and verifying the Graph permissions the tenant administrator will review...\n'
./scripts/configure-blueprint-graph-permissions.sh --blueprint-id "$BLUEPRINT_ID"

if [[ "${HERMES_INSTALL_ROUTINE_AUTOMATION:-true}" == "true" ]]; then
  printf '\nInstalling the tenant automation for future Agent Identity Foundry access...\n'
  ./scripts/install-agent-rbac-automation.sh \
    --resource-group "$RESOURCE_GROUP" \
    --account "$ACCOUNT" \
    --project "$PROJECT" \
    --blueprint-client-id "$BLUEPRINT_ID" \
    --location "$LOCATION" \
    --include-existing
fi

cat <<'EOF'

Developer provisioning is complete.

Nothing was submitted to Marketplace, AppSource, or the public Teams Store. A tenant administrator
must now open Microsoft 365 admin center > Agents > All agents > Requests, review Hermes Agent on
Foundry, choose its hiring scope and policy, grant consent, and activate it. Microsoft then owns the
managed Create instance flow and provisions each licensed Agent Identity and Agent User. The
installed tenant automation grants only the Foundry User role, at this Hermes project, to new child
Agent Identities from this blueprint so they can manage their own Foundry routines after
provisioning. Non-turn Agent OBO credentials are a separate optional routine-execution setup.
EOF
