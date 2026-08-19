#!/usr/bin/env bash
# Create a Foundry hosted-agent version from the currently serving version, changing the image and
# explicitly selected model settings. The script verifies the new definition but never routes
# traffic automatically.
#
# Documented calls, from
# https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-agent
#   create : POST  {base}/agents/{name}/versions?api-version=v1
#   route  : PATCH {base}/agents/{name}?api-version=v1   Content-Type: application/merge-patch+json
#   Every data-plane call needs --resource https://ai.azure.com or auth fails.
#
set -euo pipefail
cd "$(dirname "$0")"

: "${ACCOUNT:?Set ACCOUNT to the Foundry account name}"
: "${PROJECT:?Set PROJECT to the Foundry project name}"
: "${AGENT:?Set AGENT to the hosted-agent name}"
: "${REG:?Set REG to the Azure Container Registry name (without .azurecr.io)}"
BASE="https://${ACCOUNT}.services.ai.azure.com/api/projects/${PROJECT}"
RES="https://ai.azure.com"

TAG="${1:-}"
[ -n "$TAG" ] || { echo "usage: $0 <image-tag>   e.g. $0 v2"; exit 1; }

# Override these names to match model deployments available in the target Foundry project.
PRIMARY_MODEL="${PRIMARY_MODEL:-gpt-5.6-terra}"
FALLBACK_MODEL="${FALLBACK_MODEL:-gpt-5.6-luna}"
ROUTINE_CREDENTIAL_VAULT_URI="${ROUTINE_CREDENTIAL_VAULT_URI:-}"
ROUTINE_CREDENTIAL_CERTIFICATE_NAME="${ROUTINE_CREDENTIAL_CERTIFICATE_NAME:-}"

# Resolve the tag to an immutable digest before creating the agent version.
DIGEST=$(az acr repository show --name "$REG" --image "hermes-foundry:$TAG" \
           --query "digest" -o tsv)
[ -n "$DIGEST" ] || { echo "ERROR: no digest for hermes-foundry:$TAG in $REG"; exit 1; }
IMAGE="${REG}.azurecr.io/hermes-foundry@${DIGEST}"
echo "==> image: $IMAGE"

CUR=$(az rest --method GET --resource "$RES" \
        --url "${BASE}/agents/${AGENT}?api-version=v1" \
        --query "agent_endpoint.version_selector.version_selection_rules[0].agent_version" -o tsv)
echo "==> currently serving version: $CUR  (this is your rollback target)"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
az rest --method GET --resource "$RES" \
  --url "${BASE}/agents/${AGENT}/versions/${CUR}?api-version=v1" -o json > "$WORK/cur.json"

# Clone the serving definition, preserve its metadata and environment, and replace only the fields
# controlled by this release. The response is validated before a routing command is offered.
python3 - "$WORK/cur.json" "$IMAGE" "$PRIMARY_MODEL" "$FALLBACK_MODEL" "$ROUTINE_CREDENTIAL_VAULT_URI" "$ROUTINE_CREDENTIAL_CERTIFICATE_NAME" > "$WORK/body.json" <<'PY'
import json, sys
cur = json.load(open(sys.argv[1])); d = cur["definition"]
env = dict(d.get("environment_variables", {}) or {})
env["MODEL_DEPLOYMENT_NAME"] = sys.argv[3]
env["FALLBACK_MODEL_DEPLOYMENT_NAME"] = sys.argv[4]
if sys.argv[5] or sys.argv[6]:
    if not (sys.argv[5] and sys.argv[6]):
        raise SystemExit("Both ROUTINE_CREDENTIAL_VAULT_URI and ROUTINE_CREDENTIAL_CERTIFICATE_NAME are required")
    env["RoutineCredentialVaultUri"] = sys.argv[5]
    env["RoutineCredentialCertificateName"] = sys.argv[6]
body = {
    "definition": {
        "kind": d.get("kind", "hosted"),
        "image": sys.argv[2],
        "cpu": str(d["cpu"]),
        "memory": str(d["memory"]),
        "container_protocol_versions": d.get("container_protocol_versions", []),
        "environment_variables": env,
    },
}
# Metadata is a sibling of definition and must be preserved.
if cur.get("metadata"):
    body["metadata"] = cur["metadata"]
json.dump(body, sys.stdout)
PY

echo "==> creating a new version from $CUR (env var VALUES never printed)"
az rest --method POST --resource "$RES" \
  --url "${BASE}/agents/${AGENT}/versions?api-version=v1" \
  --body "@$WORK/body.json" -o json > "$WORK/new.json"

NEW=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['version'])" "$WORK/new.json")
echo "==> created version $NEW"

# Verify the created version before printing an opt-in routing command.
az rest --method GET --resource "$RES" \
  --url "${BASE}/agents/${AGENT}/versions/${NEW}?api-version=v1" -o json > "$WORK/newfull.json"

python3 - "$WORK/cur.json" "$WORK/newfull.json" "$CUR" "$NEW" "$PRIMARY_MODEL" "$FALLBACK_MODEL" <<'PY'
import json, sys
a = json.load(open(sys.argv[1])); b = json.load(open(sys.argv[2]))
da, db = a["definition"], b["definition"]
ea, eb = da.get("environment_variables", {}) or {}, db.get("environment_variables", {}) or {}
print(f"\n---- version {sys.argv[3]} (serving)  vs  {sys.argv[4]} (new) ----")
print(f"  image      : CHANGED  {str(da.get('image'))[-24:]}  ->  {str(db.get('image'))[-24:]}")
for f in ("kind", "cpu", "memory"):
    m = "same" if da.get(f) == db.get(f) else f"*** DIFFERS: {da.get(f)} -> {db.get(f)}"
    print(f"  {f:11}: {m}  ({db.get(f)})")
pa, pb = da.get("container_protocol_versions"), db.get("container_protocol_versions")
print("  protocols  : " + ("same" if pa == pb else f"*** DIFFERS: {pa} -> {pb}"))
ma, mb = a.get("metadata"), b.get("metadata")
print("  metadata   : " + ("same" if ma == mb else f"*** DIFFERS: {ma} -> {mb}") + f"  ({mb})")
# Names and value-identity only. Values are secrets (IdentitySignKey among them) and are NEVER printed.
missing, added = sorted(set(ea) - set(eb)), sorted(set(eb) - set(ea))
changed = sorted(k for k in set(ea) & set(eb) if ea[k] != eb[k])
print(f"  env vars   : {len(ea)} -> {len(eb)}")
if missing: print(f"     *** DROPPED (agent will break): {missing}")
if added:   print(f"     added   : {added}")
if changed: print(f"     changed : {changed}   (names only; values not shown)")
if not (missing or added or changed): print("     identical (names and values)")
print()

# Reject incomplete definitions even if the API accepted the create request.
problems = []
if missing:                                     problems.append(f"lost environment variables: {missing}")
if not db.get("image"):                         problems.append("has NO IMAGE")
if not db.get("container_protocol_versions"):   problems.append("has NO PROTOCOLS (Teams cannot reach it)")
if eb.get("MODEL_DEPLOYMENT_NAME") != sys.argv[5]:
    problems.append("primary model environment variable was not applied")
if eb.get("FALLBACK_MODEL_DEPLOYMENT_NAME") != sys.argv[6]:
    problems.append("fallback model environment variable was not applied")
if problems:
    sys.exit("ABORT: new version " + sys.argv[4] + " " + "; ".join(problems)
             + ". Do NOT route traffic to it.")
PY

cat <<EOF
==> version $NEW created and verified. NOTHING IS ROUTED TO IT YET.

To serve it:
  az rest --method PATCH --resource $RES \\
    --url "${BASE}/agents/${AGENT}?api-version=v1" \\
    --headers "Content-Type=application/merge-patch+json" \\
    --body '{"agent_endpoint":{"version_selector":{"version_selection_rules":[{"agent_version":"$NEW","traffic_percentage":100,"type":"FixedRatio"}]}}}'

To roll back, run the same command with agent_version "$CUR".
EOF
