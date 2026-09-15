#!/usr/bin/env bash
# Safely activate or roll back one hosted-agent version. The expected-current guard prevents two
# operators/releases from silently routing over one another.
set -euo pipefail
cd "$(dirname "$0")/.."

usage() {
  cat <<'EOF'
Usage:
  ./scripts/route-agent-version.sh \
    --account <foundry-account> \
    --project <foundry-project> \
    --agent <hosted-agent-name> \
    --target-version <version> \
    --expect-current <version>

This command validates the target and then changes the hosted-agent route. It does not create or
delete versions, permissions, publications, or Microsoft 365 instances.
EOF
}

ACCOUNT=""
PROJECT=""
AGENT=""
TARGET_VERSION=""
EXPECTED_CURRENT=""
while (($#)); do
  case "$1" in
    --account) ACCOUNT="${2:?}"; shift 2 ;;
    --project) PROJECT="${2:?}"; shift 2 ;;
    --agent) AGENT="${2:?}"; shift 2 ;;
    --target-version) TARGET_VERSION="${2:?}"; shift 2 ;;
    --expect-current) EXPECTED_CURRENT="${2:?}"; shift 2 ;;
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
[[ "$TARGET_VERSION" =~ ^[0-9]+$ ]] \
  || { printf 'ERROR: --target-version must be numeric.\n' >&2; exit 2; }
[[ "$EXPECTED_CURRENT" =~ ^[0-9]+$ ]] \
  || { printf 'ERROR: --expect-current must be numeric.\n' >&2; exit 2; }
command -v az >/dev/null || { printf 'ERROR: Azure CLI (az) is required.\n' >&2; exit 2; }
command -v python3 >/dev/null || { printf 'ERROR: Python 3 is required.\n' >&2; exit 2; }

BASE="https://${ACCOUNT}.services.ai.azure.com/api/projects/${PROJECT}"
RESOURCE="https://ai.azure.com"
WORK=$(mktemp -d /tmp/hermes-route-version.XXXXXX)
cleanup() {
  case "$WORK" in
    /tmp/hermes-route-version.*) find "$WORK" -depth -delete 2>/dev/null || true ;;
  esac
}
trap cleanup EXIT

read_agent() {
  az rest --method GET --resource "$RESOURCE" \
    --url "${BASE}/agents/${AGENT}?api-version=v1" --output json
}

read_agent > "$WORK/agent-before.json"
python3 - "$WORK/agent-before.json" "$EXPECTED_CURRENT" "$WORK/blueprint.txt" <<'PY'
import json, re, sys

agent = json.load(open(sys.argv[1]))
rules = ((agent.get("agent_endpoint") or {}).get("version_selector") or {}).get(
    "version_selection_rules"
) or []
serving = [item for item in rules if item.get("traffic_percentage") == 100]
if len(serving) != 1:
    raise SystemExit("ABORT: agent does not have exactly one 100% serving version")
current = str(serving[0].get("agent_version") or "")
if current != sys.argv[2]:
    raise SystemExit(
        f"ABORT: expected serving version {sys.argv[2]}, but Foundry currently serves {current}. "
        "Refresh state before routing; another release may have completed."
    )
blueprint = str((agent.get("blueprint") or {}).get("client_id") or "")
if not re.fullmatch(r"[0-9a-fA-F-]{36}", blueprint):
    raise SystemExit("ABORT: hosted agent has no valid managed Agent Identity blueprint")
open(sys.argv[3], "w").write(blueprint)
print(f"PASS: serving version still matches the expected version ({current}).")
PY

BLUEPRINT_ID=$(cat "$WORK/blueprint.txt")
az rest --method GET --resource "$RESOURCE" \
  --url "${BASE}/agents/${AGENT}/versions/${TARGET_VERSION}?api-version=v1" \
  --output json > "$WORK/target.json"

python3 - "$WORK/target.json" "$BLUEPRINT_ID" <<'PY'
import json, sys

version = json.load(open(sys.argv[1]))
status = str(version.get("status") or "").lower()
if status != "active":
    raise SystemExit(f"ABORT: target version status is {status or '(missing)'!r}, not active")
definition = version.get("definition") or {}
if "@sha256:" not in str(definition.get("image") or ""):
    raise SystemExit("ABORT: target image is not pinned to an immutable sha256 digest")
protocols = {item.get("protocol") for item in definition.get("container_protocol_versions") or []}
if not {"activity_protocol", "responses"}.issubset(protocols):
    raise SystemExit("ABORT: target version is missing an edge protocol")
env = definition.get("environment_variables") or {}
required = {
    "AgentLogic", "AgentTenantId", "AgentBlueprintId", "AZURE_AI_PROJECT_ENDPOINT",
    "AZURE_FOUNDRY_BASE_URL", "MODEL_DEPLOYMENT_NAME", "FALLBACK_MODEL_DEPLOYMENT_NAME",
    "MODEL_PRIORITY", "MODEL_DEPLOYMENTS_SNAPSHOT", "IdentitySignKey",
    "RoutineBindingSignKey",
}
missing = sorted(required - set(env))
if missing:
    raise SystemExit("ABORT: target version is missing required settings: " + ", ".join(missing))
if env.get("AgentLogic") != "HermesRouter":
    raise SystemExit("ABORT: target version is not using the production HermesRouter mode")
if str(env.get("AgentBlueprintId") or "").lower() != sys.argv[2].lower():
    raise SystemExit("ABORT: target version AgentBlueprintId does not match the hosted agent")
print("PASS: target version is active, digest-pinned, protocol-complete, and production-configured.")
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
    raise SystemExit("ABORT: blueprint is missing Graph inheritance: " + ", ".join(missing))
print(f"PASS: blueprint still declares all {len(required)} required Graph scopes.")
PY

BODY=$(python3 - "$TARGET_VERSION" <<'PY'
import json, sys
print(json.dumps({"agent_endpoint": {"version_selector": {"version_selection_rules": [{
    "agent_version": sys.argv[1], "traffic_percentage": 100, "type": "FixedRatio"
}]}}}))
PY
)

set +e
az rest --method PATCH --resource "$RESOURCE" \
  --url "${BASE}/agents/${AGENT}?api-version=v1" \
  --headers "Content-Type=application/merge-patch+json" \
  --body "$BODY" --output none
PATCH_STATUS=$?
set -e

# A failed or interrupted PATCH has an uncertain outcome. Always read the route back instead of
# telling the operator to blindly replay it.
read_agent > "$WORK/agent-after.json"
python3 - "$WORK/agent-after.json" "$TARGET_VERSION" "$PATCH_STATUS" <<'PY'
import json, sys

agent = json.load(open(sys.argv[1]))
rules = ((agent.get("agent_endpoint") or {}).get("version_selector") or {}).get(
    "version_selection_rules"
) or []
serving = [item for item in rules if item.get("traffic_percentage") == 100]
actual = str(serving[0].get("agent_version") or "") if len(serving) == 1 else ""
if actual != sys.argv[2]:
    suffix = " The PATCH command also returned an error." if sys.argv[3] != "0" else ""
    raise SystemExit(
        f"FAIL: routing verification expected version {sys.argv[2]}, but Foundry serves "
        f"{actual or '(no unambiguous version)'}.{suffix} Do not retry without refreshing state."
    )
if sys.argv[3] != "0":
    print("PASS: the PATCH command reported an error, but read-back proves the target is serving.")
else:
    print("PASS: routing update succeeded and read-back confirms the target is serving.")
PY

printf 'Hosted agent %s now serves version %s. Previous version %s remains available for rollback.\n' \
  "$AGENT" "$TARGET_VERSION" "$EXPECTED_CURRENT"
