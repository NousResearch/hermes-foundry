#!/usr/bin/env bash
# Read-only validation of an existing Hermes Foundry deployment. Values of hosted-agent environment
# variables are never printed or copied outside the temporary directory.
set -euo pipefail
cd "$(dirname "$0")/.."

usage() {
  cat <<'EOF'
Usage:
  ./scripts/validate-deployment.sh \
    --account <foundry-account> \
    --project <foundry-project> \
    --agent <hosted-agent-name> \
    [--bot-service-id <azure-resource-id>]

This command performs read-only Azure, Foundry, and Graph checks. It creates no resource, role,
permission, publication, or agent instance.
EOF
}

ACCOUNT=""
PROJECT=""
AGENT=""
BOT_SERVICE_ID=""
while (($#)); do
  case "$1" in
    --account) ACCOUNT="${2:?}"; shift 2 ;;
    --project) PROJECT="${2:?}"; shift 2 ;;
    --agent) AGENT="${2:?}"; shift 2 ;;
    --bot-service-id) BOT_SERVICE_ID="${2:?}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ "$ACCOUNT" =~ ^[A-Za-z0-9][A-Za-z0-9-]{1,62}[A-Za-z0-9]$ ]] \
  || { printf 'ERROR: unsafe or missing --account.\n' >&2; exit 2; }
[[ "$PROJECT" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{1,126}[A-Za-z0-9]$ ]] \
  || { printf 'ERROR: unsafe or missing --project.\n' >&2; exit 2; }
[[ "$AGENT" =~ ^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$ ]] \
  || { printf 'ERROR: unsafe or missing --agent.\n' >&2; exit 2; }
command -v az >/dev/null || { printf 'ERROR: Azure CLI (az) is required.\n' >&2; exit 2; }
command -v python3 >/dev/null || { printf 'ERROR: Python 3 is required.\n' >&2; exit 2; }

BASE="https://${ACCOUNT}.services.ai.azure.com/api/projects/${PROJECT}"
RESOURCE="https://ai.azure.com"
WORK=$(mktemp -d /tmp/hermes-deployment-validation.XXXXXX)
cleanup() {
  case "$WORK" in
    /tmp/hermes-deployment-validation.*) find "$WORK" -depth -delete 2>/dev/null || true ;;
  esac
}
trap cleanup EXIT

az rest --method GET --resource "$RESOURCE" \
  --url "${BASE}/agents/${AGENT}?api-version=v1" --output json > "$WORK/agent.json"

python3 - "$WORK/agent.json" "$WORK/selection.txt" <<'PY'
import json, re, sys

agent = json.load(open(sys.argv[1]))
endpoint = agent.get("agent_endpoint") or {}
protocols = set(endpoint.get("protocols") or [])
if not {"activity", "responses"}.issubset(protocols):
    raise SystemExit("FAIL: hosted agent must expose both activity and responses protocols")
schemes = {item.get("type") for item in endpoint.get("authorization_schemes") or []}
if "BotServiceTenant" not in schemes:
    raise SystemExit("FAIL: hosted agent is not protected by BotServiceTenant authorization")
rules = (endpoint.get("version_selector") or {}).get("version_selection_rules") or []
serving = [item for item in rules if item.get("traffic_percentage") == 100]
if len(serving) != 1 or not str(serving[0].get("agent_version") or "").strip():
    raise SystemExit("FAIL: hosted agent must have exactly one 100% serving version")
blueprint = str((agent.get("blueprint") or {}).get("client_id") or "")
if not re.fullmatch(r"[0-9a-fA-F-]{36}", blueprint):
    raise SystemExit("FAIL: hosted agent has no valid managed Agent Identity blueprint")
with open(sys.argv[2], "w") as output:
    output.write(str(serving[0]["agent_version"]) + "\n" + blueprint + "\n")
print("PASS: hosted-agent route, protocols, authorization, and blueprint binding are valid.")
PY

SERVING_VERSION=$(sed -n '1p' "$WORK/selection.txt")
BLUEPRINT_ID=$(sed -n '2p' "$WORK/selection.txt")
az rest --method GET --resource "$RESOURCE" \
  --url "${BASE}/agents/${AGENT}/versions/${SERVING_VERSION}?api-version=v1" \
  --output json > "$WORK/version.json"

python3 - "$WORK/version.json" <<'PY'
import json, sys

version = json.load(open(sys.argv[1]))
status = str(version.get("status") or "").lower()
if status and status != "active":
    raise SystemExit(f"FAIL: serving version status is {status!r}, not active")
definition = version.get("definition") or {}
image = str(definition.get("image") or "")
if "@sha256:" not in image:
    raise SystemExit("FAIL: serving image is not pinned to an immutable sha256 digest")
protocols = {
    item.get("protocol") for item in definition.get("container_protocol_versions") or []
}
if not {"activity_protocol", "responses"}.issubset(protocols):
    raise SystemExit("FAIL: serving version is missing an edge protocol")
env = definition.get("environment_variables") or {}
required = {
    "AgentLogic", "AgentTenantId", "AgentBlueprintId", "AZURE_AI_PROJECT_ENDPOINT",
    "AZURE_FOUNDRY_BASE_URL", "MODEL_DEPLOYMENT_NAME",
    "FALLBACK_MODEL_DEPLOYMENT_NAME", "MODEL_PRIORITY", "MODEL_DEPLOYMENTS_SNAPSHOT",
    "IdentitySignKey",
}
missing = sorted(required - set(env))
if missing:
    raise SystemExit(f"FAIL: serving version is missing required settings: {', '.join(missing)}")
if env.get("AgentLogic") != "HermesRouter":
    raise SystemExit("FAIL: serving version is not using the production HermesRouter mode")
print(
    "PASS: serving version is active, digest-pinned, protocol-complete, and has all required "
    f"settings ({len(env)} names checked; values not printed; routine signing uses "
    f"{'a dedicated key' if env.get('RoutineBindingSignKey') else 'the supported IdentitySignKey fallback'})."
)
PY

az rest --method GET \
  --url "https://graph.microsoft.com/v1.0/applications/${BLUEPRINT_ID}/microsoft.graph.agentIdentityBlueprint/inheritablePermissions/00000003-0000-0000-c000-000000000000" \
  --output json > "$WORK/inheritance.json"

python3 - "$WORK/inheritance.json" <<'PY'
import json, sys

required = {
    "Chat.ReadWrite", "Mail.ReadWrite", "Mail.Send", "User.Read.All",
    "User.ReadBasic.All", "Files.ReadWrite.All", "Calendars.ReadWrite",
    "Sites.ReadWrite.All",
}
payload = json.load(open(sys.argv[1]))
actual = set((payload.get("inheritableScopes") or {}).get("scopes") or [])
missing = sorted(required - actual)
if missing:
    raise SystemExit("FAIL: blueprint is missing Graph inheritance: " + ", ".join(missing))
print(f"PASS: blueprint declares all {len(required)} required Graph inheritable scopes.")
PY

if [[ -n "$BOT_SERVICE_ID" ]]; then
  az resource show --ids "$BOT_SERVICE_ID" --output json > "$WORK/bot.json"
  python3 - "$WORK/bot.json" "$BLUEPRINT_ID" <<'PY'
import json, sys

bot = json.load(open(sys.argv[1]))
properties = bot.get("properties") or {}
if str(properties.get("msaAppId") or "").lower() != sys.argv[2].lower():
    raise SystemExit("FAIL: Bot Service msaAppId does not match the agent blueprint")
endpoint = str(properties.get("endpoint") or "")
if "/protocols/activityProtocol" not in endpoint:
    raise SystemExit("FAIL: Bot Service endpoint is not the Foundry activity protocol")
print("PASS: Bot Service identity and Activity Protocol endpoint match the blueprint.")
PY
else
  printf 'INFO: --bot-service-id was not supplied; Azure Bot Service binding was not checked.\n'
fi

printf 'Deployment validation passed. No resources or permissions were changed.\n'
