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

agent_lookup="${output_dir}/existing-agent.json"
if python3 ./scripts/foundry_agent_lookup.py \
  --url "${base}/agents/${AGENT}?api-version=v1" \
  --output "$agent_lookup"; then
  LIVE_BLUEPRINT_ID=$(python3 - "$agent_lookup" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
print((data.get("blueprint") or {}).get("client_id") or "")
PY
  )
  [[ "$LIVE_BLUEPRINT_ID" =~ ^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$ ]] || {
    printf 'ERROR: existing hosted agent has no usable blueprint client ID.\n' >&2
    exit 1
  }
  live_blueprint_lower=$(printf '%s' "$LIVE_BLUEPRINT_ID" | tr '[:upper:]' '[:lower:]')
  expected_blueprint_lower=$(printf '%s' "${BLUEPRINT_ID}" | tr '[:upper:]' '[:lower:]')
  if [[ "$live_blueprint_lower" != "$expected_blueprint_lower" ]]; then
    # A known existing deployment is not a lookup failure. Leave it intact, but never adopt its
    # identity silently: the Azure template's bot/automation may refer to the planned blueprint.
    # Exit before building, publishing, configuring permissions, or installing role automation.
    cat <<EOF

Existing Hermes deployment found. Remaining setup skipped.

Hosted agent: ${AGENT}
Existing identity blueprint: ${LIVE_BLUEPRINT_ID}
Identity blueprint planned by this environment: ${BLUEPRINT_ID}

This hosted agent already uses a different identity blueprint. It has not been replaced or updated.
Image build, publication, permission changes, and routine automation were skipped by this hook.

You can find the existing blueprint at https://admin.cloud.microsoft/#/agents/all.
For an image update, use the existing-deployment update steps in README.md, not a new installation.
Review the selected Azure environment before attempting a separate installation.

Azure infrastructure provisioning ran before this check. Its changes have not been rolled back;
this message does not mean a new installation completed or that every feature is ready to use.
EOF
    exit 0
  fi
  printf '\nThe hosted agent already exists with the expected identity; preserving it and resuming publication.\n'
else
  lookup_status=$?
  if [[ "$lookup_status" -eq 4 ]]; then
    printf '\nBuilding the Hermes image in Azure Container Registry...\n'
    HERMES_BUILD_SKIP_LOCAL_CONFIG=true REG="$REG" ./build.sh "$TAG"
    printf '\nCreating the Hermes hosted agent bound to its explicit managed identity blueprint...\n'
    ./scripts/create-hosted-blueprint.sh
  else
    printf 'ERROR: hosted-agent existence could not be verified; refusing to create or alter it.\n' >&2
    exit 1
  fi
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

cat <<EOF

Developer provisioning is complete. Finish these required tenant-administrator steps in order:

1. APPROVE THE BLUEPRINT
   Open: https://admin.cloud.microsoft/#/agents/all/requested
   Select "Hermes Agent on Foundry", review its permissions and hiring scope, and click Approve.

2. ENABLE THE BLUEPRINT
   Open: https://admin.cloud.microsoft/#/agents/all
   Select "Hermes Agent on Foundry" and click Enable. Confirm that its status is Available.

3. CONNECT THE FOUNDRY BOT
   When using ./setup.sh, return to its approval prompt and press Enter after approval.
   Setup verifies approval and configures Bot Based routing through Microsoft Graph (beta).
   If you stopped setup, run ./setup.sh --continue; completed provisioning is not repeated.

   Manual fallback when running azd directly or when automatic configuration is unavailable:
   Open: https://dev.teams.microsoft.com/tools/agent-blueprint/${BLUEPRINT_ID}/configuration
   Change Agent Type from Notification/API Based to Bot Based.
   Enter this Bot ID: ${BLUEPRINT_ID}
   Click Save and confirm that the saved page still shows Bot Based and the same Bot ID.

Do not create an instance until all three steps are complete. A blank Bot ID or Notification/API
Based configuration can leave Create/Add instance waiting without completing.

Nothing was submitted to Marketplace, AppSource, or the public Teams Store. Microsoft owns the
managed Create instance flow and provisions each licensed Agent Identity and Agent User. The
installed tenant automation grants only the Foundry User role, at this Hermes project, to new child
Agent Identities from this blueprint so they can manage their own Foundry routines after
provisioning. Non-turn Agent OBO credentials are a separate optional routine-execution setup.
EOF
