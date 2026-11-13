#!/usr/bin/env bash
# Create a NEW Foundry hosted agent bound to the explicit Managed Agent Identity Blueprint created
# by the Bicep deployment. This is the same construction used by the proven hfull deployment.
set -euo pipefail
# Explicitly configured inference settings are copied into the new version; credentials are not.

cd "$(dirname "$0")/.."

[ ! -f .deployment.local ] || source ./.deployment.local
for inference_setting in RAI_POLICY_ID INFERENCE_PROVIDER INFERENCE_ALLOWED_PROVIDERS NOUS_MODEL NOUS_MODELS NOUS_INFERENCE_BASE_URL; do
  if declare -p "$inference_setting" >/dev/null 2>&1; then export "$inference_setting"; fi
done


usage() {
  cat <<'EOF'
usage: create-hosted-blueprint.sh [--dry-run]

Required environment:
  ACCOUNT          Existing Microsoft Foundry account name
  PROJECT          Existing project name
  AGENT            New technical agent name, for example hermes-agent-on-foundry
  PRIMARY_MODEL    Existing model deployment name
  FALLBACK_MODEL   Existing model deployment name (may equal PRIMARY_MODEL)
  BLUEPRINT_ID     Entra client ID returned by the explicit MAIB creation step
  MAIB_NAME        Foundry name of that Managed Agent Identity Blueprint

Image source, choose one:
  IMAGE             Full immutable ACR image reference containing @sha256:...
  REG and TAG       Resolve hermes-full-foundry:TAG in this Azure Container Registry
  SOURCE_AGENT      Reuse the immutable image of this existing agent's serving version

Optional:
  MODEL_PRIORITY               Ordered comma-separated deployment names
  MODEL_ALLOWED_DEPLOYMENTS    Runtime allowlist; unset means all compatible project deployments
  M365_WRITE_POLICY            internal (default), owner-only, or disabled
  M365_OUTBOUND_POLICY         internal (default), owner-only, or disabled
  IDLE_TIMEOUT_SECONDS         300-3600; default 300
  OUTPUT_DIR                   Default deployment-output/<AGENT>

The command creates no Azure RBAC assignments and grants no Microsoft 365 consent.
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
: "${AGENT:?Set AGENT to the NEW technical agent name}"
: "${PRIMARY_MODEL:?Set PRIMARY_MODEL to an existing deployment name}"
: "${FALLBACK_MODEL:?Set FALLBACK_MODEL to an existing deployment name}"
: "${BLUEPRINT_ID:?Set BLUEPRINT_ID from the Bicep MAIB output}"
: "${MAIB_NAME:?Set MAIB_NAME from the Bicep MAIB output}"

[[ "$AGENT" =~ ^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$ ]] || {
  echo "ERROR: AGENT must be 3-64 lowercase letters, digits, or hyphens" >&2; exit 2;
}
[[ "$PRIMARY_MODEL" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || {
  echo "ERROR: unsafe PRIMARY_MODEL deployment name" >&2; exit 2;
}
[[ "$FALLBACK_MODEL" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || {
  echo "ERROR: unsafe FALLBACK_MODEL deployment name" >&2; exit 2;
}
[[ "$BLUEPRINT_ID" =~ ^[0-9a-fA-F-]{36}$ ]] || {
  echo "ERROR: BLUEPRINT_ID must be a GUID" >&2; exit 2;
}
[[ "$MAIB_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{1,126}[A-Za-z0-9]$ ]] || {
  echo "ERROR: unsafe MAIB_NAME" >&2; exit 2;
}

IDLE_TIMEOUT_SECONDS="${IDLE_TIMEOUT_SECONDS:-300}"
if ! [[ "$IDLE_TIMEOUT_SECONDS" =~ ^[0-9]+$ ]] \
   || (( IDLE_TIMEOUT_SECONDS < 300 || IDLE_TIMEOUT_SECONDS > 3600 )); then
  echo "ERROR: IDLE_TIMEOUT_SECONDS must be from 300 through 3600" >&2; exit 2
fi

BASE="https://${ACCOUNT}.services.ai.azure.com/api/projects/${PROJECT}"
RESOURCE="https://ai.azure.com"
OUTPUT_DIR="${OUTPUT_DIR:-deployment-output/${AGENT}}"
MODEL_PRIORITY="${MODEL_PRIORITY:-${PRIMARY_MODEL},${FALLBACK_MODEL}}"
M365_WRITE_POLICY="${M365_WRITE_POLICY:-internal}"
M365_OUTBOUND_POLICY="${M365_OUTBOUND_POLICY:-internal}"
for policy in "$M365_WRITE_POLICY" "$M365_OUTBOUND_POLICY"; do
  [[ "$policy" =~ ^(internal|owner-only|disabled)$ ]] || {
    echo "ERROR: Microsoft 365 action policies must be internal, owner-only, or disabled" >&2
    exit 2
  }
done
mkdir -p "$OUTPUT_DIR"
WORK=$(mktemp -d /tmp/hermes-blueprint.XXXXXX)
cleanup() {
  # WORK is created above with a fixed /tmp/hermes-blueprint.XXXXXX template. It contains generated
  # signing material while the two versions are created and must not survive the command.
  case "$WORK" in
    /tmp/hermes-blueprint.*) find "$WORK" -depth -delete 2>/dev/null || true ;;
  esac
}
trap cleanup EXIT

if python3 ./scripts/foundry_agent_lookup.py \
    --url "${BASE}/agents/${AGENT}?api-version=v1" \
    --output "$WORK/existing-agent.json"; then
  echo "ERROR: agent '${AGENT}' already exists. This script will not alter it." >&2
  exit 1
else
  lookup_status=$?
  if [[ "$lookup_status" -ne 4 ]]; then
    echo "ERROR: agent existence could not be verified. This script will not create or alter it." >&2
    exit 1
  fi
fi

python3 scripts/inference_settings.py --validate-new

if [ -n "${IMAGE:-}" ]; then
  RESOLVED_IMAGE="$IMAGE"
elif [ -n "${REG:-}" ] && [ -n "${TAG:-}" ]; then
  DIGEST=$(az acr repository show --name "$REG" \
    --image "hermes-full-foundry:${TAG}" --query digest -o tsv)
  [ -n "$DIGEST" ] || { echo "ERROR: image tag did not resolve" >&2; exit 1; }
  RESOLVED_IMAGE="${REG}.azurecr.io/hermes-full-foundry@${DIGEST}"
elif [ -n "${SOURCE_AGENT:-}" ]; then
  az rest --method GET --resource "$RESOURCE" \
    --url "${BASE}/agents/${SOURCE_AGENT}?api-version=v1" -o json > "$WORK/source-agent.json"
  SOURCE_VERSION=$(python3 - "$WORK/source-agent.json" <<'PY'
import json, sys
j = json.load(open(sys.argv[1]))
print(j["agent_endpoint"]["version_selector"]["version_selection_rules"][0]["agent_version"])
PY
)
  az rest --method GET --resource "$RESOURCE" \
    --url "${BASE}/agents/${SOURCE_AGENT}/versions/${SOURCE_VERSION}?api-version=v1" \
    -o json > "$WORK/source-version.json"
  RESOLVED_IMAGE=$(python3 - "$WORK/source-version.json" <<'PY'
import json, sys
print((json.load(open(sys.argv[1])).get("definition") or {}).get("image") or "")
PY
)
else
  echo "ERROR: set IMAGE, REG+TAG, or SOURCE_AGENT" >&2
  exit 2
fi

[[ "$RESOLVED_IMAGE" == *@sha256:* ]] || {
  echo "ERROR: the image must resolve to an immutable @sha256 digest" >&2; exit 1;
}

TENANT_ID=$(az account show --query tenantId -o tsv)
[ -n "$TENANT_ID" ] || { echo "ERROR: Azure CLI returned no tenant ID" >&2; exit 1; }

# Snapshot only chat-capable project deployments. Runtime enumeration remains authoritative; this
# lets /model remain useful when a child identity can infer but cannot enumerate the project.
az rest --method GET --resource "$RESOURCE" \
  --url "${BASE}/deployments?api-version=v1" -o json > "$WORK/deployments.json"
MODEL_SNAPSHOT=$(python3 - "$WORK/deployments.json" <<'PY'
import json, re, sys
j = json.load(open(sys.argv[1])); safe = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
out = []
for d in j.get("value", j.get("data", [])):
    caps = d.get("capabilities") or {}
    capable = any(str(caps.get(k, "")).lower() == "true" for k in ("chat_completion", "responses", "chatCompletion"))
    name = str(d.get("name") or "").strip()
    if name and safe.fullmatch(name) and (capable or not caps) and name.lower() not in {x.lower() for x in out}:
        out.append(name)
print(",".join(out))
PY
)
[ -n "$MODEL_SNAPSHOT" ] || MODEL_SNAPSHOT="$MODEL_PRIORITY"

IDENTITY_SIGN_KEY=$(openssl rand -hex 32)
ROUTINE_BINDING_SIGN_KEY=$(openssl rand -hex 32)

python3 - "$RESOLVED_IMAGE" "$PRIMARY_MODEL" "$FALLBACK_MODEL" "$MODEL_PRIORITY" \
  "$MODEL_SNAPSHOT" "${MODEL_ALLOWED_DEPLOYMENTS:-}" "$BASE" "$TENANT_ID" "$AGENT" \
  "$IDENTITY_SIGN_KEY" "$ROUTINE_BINDING_SIGN_KEY" "$IDLE_TIMEOUT_SECONDS" \
  "$BLUEPRINT_ID" "$MAIB_NAME" "$M365_WRITE_POLICY" "$M365_OUTBOUND_POLICY" \
  > "$WORK/v1.json" <<'PY'
import json, sys
(image, primary, fallback, priority, snapshot, allow, project, tenant, agent,
 identity_key, routine_key, idle, blueprint_id, blueprint_name, write_policy,
 outbound_policy) = sys.argv[1:]
env = {
    "AgentLogic": "HermesRouter",
    "McpDiscoverySource": "Manifest",
    "AgentTenantId": tenant,
    "AZURE_AI_PROJECT_ENDPOINT": project,
    "AZURE_AI_AGENT_NAME": agent,
    "AZURE_FOUNDRY_BASE_URL": project + "/openai/v1",
    "MODEL_DEPLOYMENT_NAME": primary,
    "FALLBACK_MODEL_DEPLOYMENT_NAME": fallback,
    "MODEL_PRIORITY": priority,
    "MODEL_DEPLOYMENTS_SNAPSHOT": snapshot,
    "HERMES_RELEASE_ID": image.rsplit("@", 1)[-1],
    "STATE_WRITER_PROTOCOL": "dual-v1-v2",
    "IdentitySignKey": identity_key,
    "RoutineBindingSignKey": routine_key,
    "AgentBlueprintId": blueprint_id,
    "M365_WRITE_POLICY": write_policy,
    "M365_OUTBOUND_POLICY": outbound_policy,
}
if allow:
    env["MODEL_ALLOWED_DEPLOYMENTS"] = allow
sys.path.insert(0, "scripts")
from inference_settings import apply
apply(env, new_install=True)
body = {
    "definition": {
        "kind": "hosted",
        "image": image,
        "cpu": "2",
        "memory": "4Gi",
        "container_protocol_versions": [
            {"protocol": "activity_protocol", "version": "v1"},
            {"protocol": "responses", "version": "2.0.0"},
        ],
        "environment_variables": env,
        "session_configuration": {"idle_timeout_seconds": int(idle)},
    },
    "metadata": {"enableVnextExperience": "true"},
    "description": "Hermes Agent on Foundry",
    "agent_endpoint": {"protocols": ["activity", "responses"]},
    "blueprint_reference": {
        "type": "ManagedAgentIdentityBlueprint",
        "blueprint_id": blueprint_name,
    },
}
json.dump(body, sys.stdout)
PY

echo "New hosted agent: $AGENT"
echo "Project: $ACCOUNT / $PROJECT"
echo "Models: $MODEL_PRIORITY"
echo "Session idle timeout: ${IDLE_TIMEOUT_SECONDS}s"
echo "Image: immutable digest verified"
echo "Existing agents changed: none"
echo "Azure roles or Microsoft 365 consent granted: none"
if $DRY_RUN; then
  echo "DRY RUN complete. No resource was created."
  exit 0
fi

python3 scripts/configure_inference_egress.py --body "$WORK/v1.json" --account "$ACCOUNT"

TOKEN=$(az account get-access-token --resource "$RESOURCE" --query accessToken -o tsv)
create_version() {
  local body=$1 output=$2
  curl --fail-with-body --silent --show-error \
    -X POST "${BASE}/agents/${AGENT}/versions?api-version=v1" \
    -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" \
    --data-binary "@${body}" > "$output"
}

wait_active() {
  python3 scripts/wait_foundry_state.py \
    --url "${BASE}/agents/${AGENT}/versions/$1?api-version=v1" --output "$2"
}

echo "Creating hosted-agent version bound to explicit MAIB ${MAIB_NAME}..."
create_version "$WORK/v1.json" "$WORK/v1-response.json"
V1=$(python3 - "$WORK/v1-response.json" <<'PY'
import json, sys
j=json.load(open(sys.argv[1])); print(j.get("version") or "")
PY
)
[ -n "$V1" ] || { echo "ERROR: create response contained no version" >&2; exit 1; }
wait_active "$V1" "$WORK/v1-active.json"
python3 scripts/verify_hosted_version.py "$WORK/v1.json" "$WORK/v1-active.json"
python3 scripts/configure_inference_egress.py --body "$WORK/v1.json" --verify "$WORK/v1-active.json"
python3 - "$WORK/v1-active.json" "$RESOLVED_IMAGE" <<'PY'
import json, sys
version = json.load(open(sys.argv[1]))
definition = version.get("definition") or {}
environment = definition.get("environment_variables") or {}
expected_release = sys.argv[2].rsplit("@", 1)[-1]
problems = []
if definition.get("image") != sys.argv[2]:
    problems.append("immutable image was not retained")
if environment.get("HERMES_RELEASE_ID") != expected_release:
    problems.append("state writer release identity was not retained")
if environment.get("STATE_WRITER_PROTOCOL") != "dual-v1-v2":
    problems.append("state writer compatibility protocol was not retained")
if problems:
    raise SystemExit("ERROR: hosted-agent version validation failed: " + "; ".join(problems))
PY
RETURNED_BLUEPRINT_ID=$(python3 - "$WORK/v1-active.json" <<'PY'
import json, sys
j=json.load(open(sys.argv[1])); print((j.get("blueprint") or {}).get("client_id") or "")
PY
)
returned_blueprint_lower=$(printf '%s' "$RETURNED_BLUEPRINT_ID" | tr '[:upper:]' '[:lower:]')
expected_blueprint_lower=$(printf '%s' "$BLUEPRINT_ID" | tr '[:upper:]' '[:lower:]')
[[ "$returned_blueprint_lower" == "$expected_blueprint_lower" ]] || {
  echo "ERROR: hosted agent returned blueprint ${RETURNED_BLUEPRINT_ID}, expected ${BLUEPRINT_ID}" >&2
  exit 1
}

python3 - "$V1" > "$WORK/route.json" <<'PY'
import json, sys
json.dump({"agent_endpoint": {
    "protocols": ["activity", "responses"],
    "authorization_schemes": [{"type": "BotServiceTenant"}],
    "version_selector": {"version_selection_rules": [{
        "agent_version": sys.argv[1], "traffic_percentage": 100, "type": "FixedRatio"
    }]},
}}, sys.stdout)
PY
az rest --method PATCH --resource "$RESOURCE" \
  --url "${BASE}/agents/${AGENT}?api-version=v1" \
  --headers Content-Type=application/merge-patch+json \
  --body "@$WORK/route.json" >/dev/null

python3 - "$OUTPUT_DIR/blueprint.json" "$AGENT" "$V1" "$BLUEPRINT_ID" "$MAIB_NAME" "$BASE" "$TENANT_ID" <<'PY'
import json, sys
path, agent, version, blueprint, blueprint_name, endpoint, tenant = sys.argv[1:]
with open(path, "w") as stream:
    json.dump({"agentName": agent, "agentVersion": version, "blueprintId": blueprint,
               "blueprintName": blueprint_name, "projectEndpoint": endpoint,
               "tenantId": tenant}, stream, indent=2)
    stream.write("\n")
PY
chmod 600 "$OUTPUT_DIR/blueprint.json"

echo "Created new Foundry hosted agent bound to the explicit blueprint successfully."
echo "Local deployment record: $OUTPUT_DIR/blueprint.json (gitignored, mode 600)"
echo "Next: publish through Foundry's native Microsoft 365 publication API."
