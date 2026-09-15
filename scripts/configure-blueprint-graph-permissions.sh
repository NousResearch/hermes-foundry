#!/usr/bin/env bash
# Declare Hermes' Microsoft Graph scopes on a Foundry-created blueprint and make those scopes
# inheritable by its child Agent Identities. This does NOT grant admin consent; the tenant admin
# reviews and grants consent in the Microsoft 365 publication wizard.
set -euo pipefail

BLUEPRINT_ID="${BLUEPRINT_ID:-}"
if [ "${1:-}" = "--blueprint-id" ]; then
  BLUEPRINT_ID="${2:-}"
elif [ -n "${1:-}" ]; then
  echo "usage: $0 --blueprint-id <guid>" >&2; exit 2
fi
[[ "$BLUEPRINT_ID" =~ ^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$ ]] || {
  echo "ERROR: BLUEPRINT_ID must be a GUID" >&2; exit 2;
}

GRAPH_APP_ID="00000003-0000-0000-c000-000000000000"
SCOPES=(
  Chat.ReadWrite
  Mail.ReadWrite
  Mail.Send
  Calendars.ReadWrite
  Files.ReadWrite.All
  Sites.ReadWrite.All
  User.Read.All
  User.ReadBasic.All
)

WORK=$(mktemp -d /tmp/hermes-blueprint-permissions.XXXXXX)
cleanup() {
  case "$WORK" in
    /tmp/hermes-blueprint-permissions.*) find "$WORK" -depth -delete 2>/dev/null || true ;;
  esac
}
trap cleanup EXIT
TOKEN=$(az account get-access-token --resource https://graph.microsoft.com/ --query accessToken -o tsv)
[ -n "$TOKEN" ] || { echo "ERROR: could not obtain a Microsoft Graph token" >&2; exit 1; }
AUTH="Authorization: Bearer ${TOKEN}"
BLUEPRINT_URI="https://graph.microsoft.com/v1.0/applications/microsoft.graph.agentIdentityBlueprint/${BLUEPRINT_ID}"

curl --fail-with-body --silent --show-error "$BLUEPRINT_URI?\$select=id,appId,requiredResourceAccess" \
  -H "$AUTH" -H "Accept: application/json" > "$WORK/blueprint.json"
curl --fail-with-body --silent --show-error \
  "https://graph.microsoft.com/v1.0/servicePrincipals(appId='${GRAPH_APP_ID}')?\$select=id,oauth2PermissionScopes" \
  -H "$AUTH" -H "Accept: application/json" > "$WORK/graph-sp.json"

python3 - "$WORK/blueprint.json" "$WORK/graph-sp.json" "${SCOPES[@]}" \
  > "$WORK/rra-patch.json" <<'PY'
import json, sys
blueprint = json.load(open(sys.argv[1])); graph = json.load(open(sys.argv[2])); wanted = sys.argv[3:]
ids = {s.get("value"): s.get("id") for s in graph.get("oauth2PermissionScopes", [])}
missing = [scope for scope in wanted if not ids.get(scope)]
if missing:
    raise SystemExit("Microsoft Graph did not publish delegated scopes: " + ", ".join(missing))
entries = list(blueprint.get("requiredResourceAccess") or [])
graph_entry = next((e for e in entries if e.get("resourceAppId") == "00000003-0000-0000-c000-000000000000"), None)
if graph_entry is None:
    graph_entry = {"resourceAppId": "00000003-0000-0000-c000-000000000000", "resourceAccess": []}
    entries.append(graph_entry)
seen = {(x.get("id"), x.get("type")) for x in graph_entry.get("resourceAccess", [])}
for scope in wanted:
    key = (ids[scope], "Scope")
    if key not in seen:
        graph_entry.setdefault("resourceAccess", []).append({"id": ids[scope], "type": "Scope"})
json.dump({"requiredResourceAccess": entries}, sys.stdout)
PY

curl --fail-with-body --silent --show-error -X PATCH "$BLUEPRINT_URI" \
  -H "$AUTH" -H "Content-Type: application/json" -H "OData-Version: 4.0" \
  --data-binary "@$WORK/rra-patch.json" >/dev/null

INHERITANCE_URI="${BLUEPRINT_URI}/inheritablePermissions/${GRAPH_APP_ID}"
python3 - "${SCOPES[@]}" > "$WORK/inheritance.json" <<'PY'
import json, sys
json.dump({
    "inheritableScopes": {
        "@odata.type": "#microsoft.graph.enumeratedScopes",
        "scopes": sys.argv[1:]
    }
}, sys.stdout)
PY

STATUS=$(curl --silent --output "$WORK/current-inheritance.json" --write-out '%{http_code}' \
  "$INHERITANCE_URI" -H "$AUTH" -H "Accept: application/json")
if [ "$STATUS" = 200 ]; then
  python3 ./scripts/merge_inheritable_scopes.py \
    "$WORK/current-inheritance.json" "${SCOPES[@]}" > "$WORK/inheritance-patch.json"
  curl --fail-with-body --silent --show-error -X PATCH "$INHERITANCE_URI" \
    -H "$AUTH" -H "Content-Type: application/json" -H "OData-Version: 4.0" \
    --data-binary "@$WORK/inheritance-patch.json" >/dev/null
elif [ "$STATUS" = 404 ]; then
  python3 - "$GRAPH_APP_ID" "${SCOPES[@]}" > "$WORK/inheritance-create.json" <<'PY'
import json, sys
json.dump({
    "resourceAppId": sys.argv[1],
    "inheritableScopes": {
        "@odata.type": "#microsoft.graph.enumeratedScopes",
        "scopes": sys.argv[2:]
    }
}, sys.stdout)
PY
  curl --fail-with-body --silent --show-error -X POST \
    "${BLUEPRINT_URI}/inheritablePermissions" \
    -H "$AUTH" -H "Content-Type: application/json" -H "OData-Version: 4.0" \
    --data-binary "@$WORK/inheritance-create.json" >/dev/null
else
  echo "ERROR: Graph returned HTTP $STATUS while reading blueprint inheritance" >&2
  exit 1
fi

# Verify the two configuration layers without creating an OAuth2 permission grant.
curl --fail-with-body --silent --show-error "$BLUEPRINT_URI?\$select=requiredResourceAccess" \
  -H "$AUTH" -H "Accept: application/json" > "$WORK/verify-blueprint.json"
curl --fail-with-body --silent --show-error "$INHERITANCE_URI" \
  -H "$AUTH" -H "Accept: application/json" > "$WORK/verify-inheritance.json"
python3 - "$WORK/verify-blueprint.json" "$WORK/graph-sp.json" "$WORK/verify-inheritance.json" \
  "${SCOPES[@]}" <<'PY'
import json, sys
blueprint, graph, inheritance = (json.load(open(p)) for p in sys.argv[1:4]); wanted = set(sys.argv[4:])
ids = {s.get("value"): s.get("id") for s in graph.get("oauth2PermissionScopes", [])}
entry = next((e for e in blueprint.get("requiredResourceAccess", []) if e.get("resourceAppId") == "00000003-0000-0000-c000-000000000000"), {})
declared_ids = {x.get("id") for x in entry.get("resourceAccess", []) if x.get("type") == "Scope"}
declared = {name for name in wanted if ids.get(name) in declared_ids}
inherited = set((inheritance.get("inheritableScopes") or {}).get("scopes") or [])
missing_declared = sorted(wanted - declared); missing_inherited = sorted(wanted - inherited)
if missing_declared or missing_inherited:
    raise SystemExit(f"Verification failed: declaration missing={missing_declared}; inheritance missing={missing_inherited}")
print("Blueprint Graph declaration and enumerated inheritance verified:")
for scope in sorted(wanted): print("  " + scope)
print("Admin consent has NOT been granted by this command.")
PY
