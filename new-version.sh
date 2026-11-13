#!/usr/bin/env bash
# Create a new hfull agent version from the CURRENTLY SERVING one, changing the image and the
# explicitly declared model configuration below.
#
# ⛔ SCOPE: this script targets `hfull` and nothing else. It never touches `.fndr` (frozen),
#    `ha365`, `hprobe` or any other stack.
#
# WHY THIS EXISTS. Twenty agent versions were created on hfull by hand and the payload was never
# written down, so "how do we ship a new image" had to be re-derived from a GET response every
# time — and the GET response is NOT the same shape as the POST body, which is exactly the kind of
# detail that gets guessed wrong at 2am. It also encodes the one mistake already made once on the
# thin stack: cloning `definition` alone silently dropped `metadata.enableVnextExperience`, because
# metadata is a SIBLING of definition, not a child.
#
# Documented calls, from
# https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-agent
#   create : POST  {base}/agents/{name}/versions?api-version=v1
#   route  : PATCH {base}/agents/{name}?api-version=v1   Content-Type: application/merge-patch+json
#   Every data-plane call needs --resource https://ai.azure.com or auth fails.
#
# THIS SCRIPT DELIBERATELY DOES NOT SWITCH TRAFFIC. It creates the version and diffs it against the
# one now serving, so the change can be inspected before anyone is exposed to it. Routing is a
# separate, explicit command, printed at the end.
set -euo pipefail
# Explicitly configured inference settings are copied into the new version; credentials are not.

cd "$(dirname "$0")"

# Optional, gitignored per-installation settings. Never commit tenant resource coordinates here.
if [[ "${HERMES_DEPLOY_SKIP_LOCAL_CONFIG:-false}" != "true" && -f .deployment.local ]]; then
  source ./.deployment.local
fi
for inference_setting in RAI_POLICY_ID INFERENCE_PROVIDER INFERENCE_ALLOWED_PROVIDERS NOUS_MODEL NOUS_MODELS NOUS_INFERENCE_BASE_URL; do
  if declare -p "$inference_setting" >/dev/null 2>&1; then export "$inference_setting"; fi
done

: "${ACCOUNT:?Set ACCOUNT to the Foundry account name (or add it to .deployment.local)}"
: "${PROJECT:?Set PROJECT to the Foundry project name (or add it to .deployment.local)}"
: "${AGENT:?Set AGENT to the hosted agent name (or add it to .deployment.local)}"
: "${REG:?Set REG to the Azure Container Registry name (or add it to .deployment.local)}"
BASE="https://${ACCOUNT}.services.ai.azure.com/api/projects/${PROJECT}"
RES="https://ai.azure.com"

TAG="${1:-}"
[ -n "$TAG" ] || { echo "usage: $0 <image-tag>   e.g. $0 v19"; exit 1; }

# Model choices are explicit overrides or inherited from the serving version below.
if [ "${MODEL_ALLOWED_DEPLOYMENTS+x}" = x ]; then
  MODEL_ALLOWLIST_MODE="set"
else
  MODEL_ALLOWLIST_MODE="preserve"
fi
MODEL_ALLOWED_DEPLOYMENTS="${MODEL_ALLOWED_DEPLOYMENTS:-}"
# Hosted agents have implicit inference access through their own Foundry PROJECT endpoint. Calling
# the account-level cognitiveservices endpoint instead bypasses that implicit path and forces every
# Agent 365 instance identity to receive a separate Azure RBAC grant after it is created. That makes
# admin-centre/blueprint-only onboarding impossible. Keep this overridable for a controlled rollback,
# but make the zero-touch project route the full-Foundry default.
PROJECT_MODEL_BASE_URL="${PROJECT_MODEL_BASE_URL:-${BASE}/openai/v1}"
ROUTINE_CREDENTIAL_VAULT_URI="${ROUTINE_CREDENTIAL_VAULT_URI:-}"
ROUTINE_CREDENTIAL_CERTIFICATE_NAME="${ROUTINE_CREDENTIAL_CERTIFICATE_NAME:-}"
M365_WRITE_POLICY="${M365_WRITE_POLICY:-}"
M365_OUTBOUND_POLICY="${M365_OUTBOUND_POLICY:-}"
for policy in "$M365_WRITE_POLICY" "$M365_OUTBOUND_POLICY"; do
  [ -z "$policy" ] || [[ "$policy" =~ ^(internal|owner-only|disabled)$ ]] || {
    echo "ERROR: Microsoft 365 action policies must be internal, owner-only, or disabled" >&2
    exit 2
  }
done

# Pin by DIGEST, never by tag. A mutable tag once moved Hermes two releases forward inside what was
# supposed to be an unrelated deploy; a digest cannot do that.
DIGEST=$(az acr repository show --name "$REG" --image "hermes-full-foundry:$TAG" \
           --query "digest" -o tsv)
[ -n "$DIGEST" ] || { echo "ERROR: no digest for hermes-full-foundry:$TAG in $REG"; exit 1; }
IMAGE="${REG}.azurecr.io/hermes-full-foundry@${DIGEST}"
echo "==> image: $IMAGE"

CUR=$(az rest --method GET --resource "$RES" \
        --url "${BASE}/agents/${AGENT}?api-version=v1" \
        --query "agent_endpoint.version_selector.version_selection_rules[0].agent_version" -o tsv)
# Resume a previously accepted create without issuing another POST.
SAVED_NEW=""
if [[ -n "${HERMES_VERSION_RESULT_FILE:-}" && -s "$HERMES_VERSION_RESULT_FILE" ]]; then
  SAVED_FIELDS=$(python3 - "$HERMES_VERSION_RESULT_FILE" "$IMAGE" "$CUR" <<'PYRESUME'
import json, sys
record = json.load(open(sys.argv[1]))
if record.get('image') != sys.argv[2] or sys.argv[3] not in {record.get('previous'), record.get('version')}:
    raise SystemExit('ABORT: saved version does not match this image or current route')
if not str(record.get('version', '')).isdigit() or not str(record.get('previous', '')).isdigit():
    raise SystemExit('ABORT: invalid saved version result')
print(record['previous'], record['version'])
PYRESUME
)
  read -r CUR SAVED_NEW <<< "$SAVED_FIELDS"
fi
echo "==> currently serving version: $CUR  (this is your rollback target)"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
az rest --method GET --resource "$RES" \
  --url "${BASE}/agents/${AGENT}/versions/${CUR}?api-version=v1" -o json > "$WORK/cur.json"

# Preserve the deployed model choice unless the operator explicitly supplies a replacement.
PRIMARY_MODEL="${PRIMARY_MODEL:-$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["definition"]["environment_variables"].get("MODEL_DEPLOYMENT_NAME", ""))' "$WORK/cur.json")}"
FALLBACK_MODEL="${FALLBACK_MODEL:-$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["definition"]["environment_variables"].get("FALLBACK_MODEL_DEPLOYMENT_NAME", ""))' "$WORK/cur.json")}"
: "${PRIMARY_MODEL:?Select PRIMARY_MODEL for the Foundry deployment}"
FALLBACK_MODEL="${FALLBACK_MODEL:-$PRIMARY_MODEL}"
MODEL_PRIORITY="${MODEL_PRIORITY:-${PRIMARY_MODEL},${FALLBACK_MODEL}}"

# A hosted-agent image release must be permission-neutral. It was not always so: on 2026-08-19 the
# Foundry release workflow reset the blueprint to its default four Graph inheritable scopes. Entra's
# audit record names Azure Machine Learning but does not distinguish version activation from the
# following M365 publication. Chat and identity kept working, but OneDrive changed to 404/403. Verify
# before creating a version so a pre-existing regression does not get mistaken for an image problem,
# then verify again afterward so a future service-side reconciliation cannot pass unnoticed.
verify_graph_inheritance() {
  local version_json="$1" stage="$2" blueprint out
  blueprint=$(python3 -c \
    "import json,sys; print((json.load(open(sys.argv[1])).get('blueprint') or {}).get('client_id') or '')" \
    "$version_json")
  [ -n "$blueprint" ] || { echo "ABORT: $stage version has no blueprint client id"; return 1; }
  out="$WORK/graph-inheritance-$stage.json"
  az rest --method GET \
    --url "https://graph.microsoft.com/v1.0/applications/${blueprint}/microsoft.graph.agentIdentityBlueprint/inheritablePermissions/00000003-0000-0000-c000-000000000000" \
    -o json > "$out"
  python3 - "$out" "$stage" <<'PY'
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
    raise SystemExit(
        f"ABORT: {sys.argv[2]} blueprint is missing Graph inheritable scopes: {missing}. "
        "Do not publish or route another version; run the documented permission reconciliation."
    )
print(f"  Graph inheritance ({sys.argv[2]}): verified {len(required)} required scopes")
PY
}

verify_graph_inheritance "$WORK/cur.json" "serving-v$CUR"

# Snapshot the project-scoped model inventory through the documented Foundry Deployments API. The
# hosted runtime also attempts live discovery, but some identity configurations allow project
# inference without project enumeration. This snapshot preserves zero-touch instance onboarding:
# the tenant admin who creates the version records the chat deployments once, and Agent Users need
# no extra role merely to render /model. It is not an authorization bypass; the live inference
# endpoint still rejects a deployment that an administrator later removes.
MODEL_DEPLOYMENT_PAGES=()
MODEL_DEPLOYMENTS_NEXT="${BASE}/deployments?api-version=v1"
MODEL_DEPLOYMENTS_PAGE=0
while [ -n "$MODEL_DEPLOYMENTS_NEXT" ]; do
  MODEL_DEPLOYMENTS_PAGE=$((MODEL_DEPLOYMENTS_PAGE + 1))
  [ "$MODEL_DEPLOYMENTS_PAGE" -le 20 ] || {
    echo "ABORT: Foundry model inventory exceeded the 20-page safety limit"
    exit 1
  }
  MODEL_PAGE_FILE="$WORK/model-deployments-${MODEL_DEPLOYMENTS_PAGE}.json"
  az rest --method GET --resource "$RES" --url "$MODEL_DEPLOYMENTS_NEXT" -o json > "$MODEL_PAGE_FILE"
  MODEL_DEPLOYMENT_PAGES+=("$MODEL_PAGE_FILE")
  MODEL_DEPLOYMENTS_NEXT=$(python3 - "$MODEL_PAGE_FILE" "$BASE" <<'PY'
import json, sys, urllib.parse
data = json.load(open(sys.argv[1]))
value = data.get("nextLink") or data.get("@odata.nextLink") or ""
if value:
    url = urllib.parse.urlparse(value)
    base = urllib.parse.urlparse(sys.argv[2])
    if url.scheme != "https" or url.hostname != base.hostname:
        raise SystemExit("ABORT: Foundry returned an unsafe deployments nextLink")
print(value)
PY
)
done
MODEL_DEPLOYMENTS_SNAPSHOT=$(python3 - "${MODEL_DEPLOYMENT_PAGES[@]}" <<'PY'
import json, re, sys
safe = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
models = []
seen = set()
for path in sys.argv[1:]:
    data = json.load(open(path))
    for deployment in data.get("value", []):
        caps = deployment.get("capabilities") or {}
        capable = any(str(caps.get(k, "")).lower() == "true"
                      for k in ("chat_completion", "responses", "chatCompletion"))
        name = str(deployment.get("name") or "").strip()
        if (deployment.get("type") == "ModelDeployment" and capable
                and safe.fullmatch(name) and name.lower() not in seen):
            seen.add(name.lower())
            models.append(name)
if not models:
    raise SystemExit("ABORT: Foundry returned no chat-capable model deployments")
print(",".join(models))
PY
)
echo "==> captured $(awk -F, '{print NF}' <<< "$MODEL_DEPLOYMENTS_SNAPSHOT") chat-capable model deployments; priority=$MODEL_PRIORITY"

# Build the POST body by cloning the serving version's definition and swapping ONLY the image.
#
# ⚠️ DO NOT USE THE FIELD NAMES FROM THE DOC'S EXAMPLE. The published sample body at
# learn.microsoft.com/.../manage-hosted-agent uses `container_configuration: {image}` and
# `protocol_versions`. Against this api-version those two keys are SILENTLY IGNORED — measured
# 2026-08-18: a POST built that way returned 200, provisioned to status `active`, and produced
# version 21 with `image: None` and `container_protocol_versions: []`. Every other field survived,
# and every other field's name matches the GET shape. So the request shape mirrors the RESPONSE
# shape: `definition.image` and `definition.container_protocol_versions`.
#
# An unroutable version is the harmless outcome. The dangerous one is that the API accepts a
# malformed body without complaint, which is exactly why the diff below runs before any traffic
# moves — an agent serving a version with no protocols would simply stop answering Teams.
python3 - "$WORK/cur.json" "$IMAGE" "$PRIMARY_MODEL" "$FALLBACK_MODEL" "$PROJECT_MODEL_BASE_URL" "$ROUTINE_CREDENTIAL_VAULT_URI" "$ROUTINE_CREDENTIAL_CERTIFICATE_NAME" "$MODEL_PRIORITY" "$MODEL_DEPLOYMENTS_SNAPSHOT" "$MODEL_ALLOWLIST_MODE" "$MODEL_ALLOWED_DEPLOYMENTS" "$M365_WRITE_POLICY" "$M365_OUTBOUND_POLICY" > "$WORK/body.json" <<'PY'
import json, sys
cur = json.load(open(sys.argv[1])); d = cur["definition"]
env = dict(d.get("environment_variables", {}) or {})
# v57 briefly tested a human-owner Bot OAuth/OBO design. Agent OBO needs none of that: a routine
# selects the child Agent Identity and paired Agent User directly. Remove only those known test keys
# when cloning the serving definition so they cannot become accidental deployment requirements.
for obsolete in (
    "OwnerOboProofEnabled",
    "OwnerOboSaasClientId",
    "Connections__BotTokenServiceConnection__Settings__ClientId",
    "Connections__BotTokenServiceConnection__Settings__AuthorityEndpoint",
    "Connections__BotTokenServiceConnection__Settings__FederatedClientId",
):
    env.pop(obsolete, None)
env["MODEL_DEPLOYMENT_NAME"] = sys.argv[3]
env["FALLBACK_MODEL_DEPLOYMENT_NAME"] = sys.argv[4]
env["AZURE_FOUNDRY_BASE_URL"] = sys.argv[5]
env["MODEL_PRIORITY"] = sys.argv[8]
env["MODEL_DEPLOYMENTS_SNAPSHOT"] = sys.argv[9]
# State writers identify their immutable runtime release without exposing tenant or agent data.
# dual-v1-v2 keeps sessions pinned to release 80 interoperable during the migration window.
env["HERMES_RELEASE_ID"] = sys.argv[2].rsplit("@", 1)[-1]
env["STATE_WRITER_PROTOCOL"] = "dual-v1-v2"
if sys.argv[10] == "set":
    if sys.argv[11]:
        env["MODEL_ALLOWED_DEPLOYMENTS"] = sys.argv[11]
    else:
        env.pop("MODEL_ALLOWED_DEPLOYMENTS", None)
if sys.argv[12]:
    env["M365_WRITE_POLICY"] = sys.argv[12]
if sys.argv[13]:
    env["M365_OUTBOUND_POLICY"] = sys.argv[13]
if sys.argv[6] or sys.argv[7]:
    if not (sys.argv[6] and sys.argv[7]):
        raise SystemExit("Both ROUTINE_CREDENTIAL_VAULT_URI and ROUTINE_CREDENTIAL_CERTIFICATE_NAME are required")
    env["RoutineCredentialVaultUri"] = sys.argv[6]
    env["RoutineCredentialCertificateName"] = sys.argv[7]
sys.path.insert(0, "scripts")
from inference_settings import apply
apply(env)
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
if d.get("session_configuration"):
    body["definition"]["session_configuration"] = d["session_configuration"]
if d.get("rai_config"):
    body["definition"]["rai_config"] = d["rai_config"]
# metadata is a SIBLING of definition. Dropping it once cost a version that looked identical and
# behaved differently, because enableVnextExperience silently went missing.
if cur.get("metadata"):
    body["metadata"] = cur["metadata"]
json.dump(body, sys.stdout)
PY

python3 scripts/configure_inference_egress.py --body "$WORK/body.json" --account "$ACCOUNT"

if [[ -n "$SAVED_NEW" ]]; then
  NEW="$SAVED_NEW"
  echo "==> resuming validation of previously created version $NEW"
else
  echo "==> creating a new version from $CUR (env var VALUES never printed)"
  if [[ -n "${HERMES_VERSION_RESULT_FILE:-}" ]]; then
    python3 - "$HERMES_VERSION_RESULT_FILE.pending" <<'PYPENDING'
import sys
from pathlib import Path
sys.path.insert(0, 'scripts')
from reinstall_agent import save
save(Path(sys.argv[1]), {'creation_requested': True})
PYPENDING
  fi
  az rest --method POST --resource "$RES" \
    --url "${BASE}/agents/${AGENT}/versions?api-version=v1" \
    --body "@$WORK/body.json" -o json > "$WORK/new.json"
  NEW=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['version'])" "$WORK/new.json")
  echo "==> created version $NEW"
  if [[ -n "${HERMES_VERSION_RESULT_FILE:-}" ]]; then
    python3 - "$HERMES_VERSION_RESULT_FILE" "$CUR" "$NEW" "$IMAGE" <<'PYSAVE'
import sys
from pathlib import Path
sys.path.insert(0, 'scripts')
from reinstall_agent import save
save(Path(sys.argv[1]), dict(previous=sys.argv[2], version=sys.argv[3], image=sys.argv[4], verified=False))
PYSAVE
  fi
fi

# VERIFY BY DIFF before anything is routed to it. A create that silently dropped environment
# variables would look like a success and fail only at runtime, as an agent that can no longer
# authenticate — the most expensive kind of failure to diagnose.
python3 scripts/wait_foundry_state.py \
  --url "${BASE}/agents/${AGENT}/versions/${NEW}?api-version=v1" --output "$WORK/newfull.json"

python3 scripts/verify_hosted_version.py "$WORK/body.json" "$WORK/newfull.json"

verify_graph_inheritance "$WORK/newfull.json" "new-v$NEW"

python3 scripts/configure_inference_egress.py --body "$WORK/body.json" --verify "$WORK/newfull.json"

python3 - "$WORK/cur.json" "$WORK/newfull.json" "$CUR" "$NEW" "$PRIMARY_MODEL" "$FALLBACK_MODEL" "$PROJECT_MODEL_BASE_URL" "$MODEL_PRIORITY" "$MODEL_DEPLOYMENTS_SNAPSHOT" "$MODEL_ALLOWLIST_MODE" "$MODEL_ALLOWED_DEPLOYMENTS" "$M365_WRITE_POLICY" "$M365_OUTBOUND_POLICY" <<'PY'
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
# These five v57-only keys belong to the abandoned human-owner OBO proof and are removed while the
# request body is built above. Every other missing key remains a release-blocking configuration loss.
expected_removed = {
    "NOUS_API_KEY_FILE", "NOUS_API_KEY_SECRET_URI",
    "OwnerOboProofEnabled",
    "OwnerOboSaasClientId",
    "Connections__BotTokenServiceConnection__Settings__ClientId",
    "Connections__BotTokenServiceConnection__Settings__AuthorityEndpoint",
    "Connections__BotTokenServiceConnection__Settings__FederatedClientId",
}
sys.path.insert(0, "scripts")
from inference_settings import apply, KEYS
expected_inference = apply(dict(ea))
expected_removed.update(k for k in KEYS if k not in expected_inference)
all_missing = set(ea) - set(eb)
removed = sorted(all_missing & expected_removed)
missing = sorted(all_missing - expected_removed)
added = sorted(set(eb) - set(ea))
changed = sorted(k for k in set(ea) & set(eb) if ea[k] != eb[k])
print(f"  env vars   : {len(ea)} -> {len(eb)}")
if missing: print(f"     *** DROPPED (agent will break): {missing}")
if removed: print(f"     removed obsolete owner-OBO proof settings: {removed}")
if added:   print(f"     added   : {added}")
if changed: print(f"     changed : {changed}   (names only; values not shown)")
if not (missing or removed or added or changed): print("     identical (names and values)")
print()

# Fail loudly on a version the API accepted but did not actually populate. Version 21 was created
# exactly this way — 200, active, and useless — and would have taken the agent off Teams if it had
# been routed to. Cheap to check, expensive to miss.
problems = []
for key in KEYS:
    if eb.get(key) != expected_inference.get(key):
        problems.append("inference setting was not applied: " + key)
if missing:                                     problems.append(f"lost environment variables: {missing}")
if not db.get("image"):                         problems.append("has NO IMAGE")
if not db.get("container_protocol_versions"):   problems.append("has NO PROTOCOLS (Teams cannot reach it)")
if eb.get("MODEL_DEPLOYMENT_NAME") != sys.argv[5]:
    problems.append("primary model environment variable was not applied")
if eb.get("FALLBACK_MODEL_DEPLOYMENT_NAME") != sys.argv[6]:
    problems.append("fallback model environment variable was not applied")
if eb.get("AZURE_FOUNDRY_BASE_URL") != sys.argv[7]:
    problems.append("project model endpoint environment variable was not applied")
if eb.get("MODEL_PRIORITY") != sys.argv[8]:
    problems.append("model priority environment variable was not applied")
if eb.get("MODEL_DEPLOYMENTS_SNAPSHOT") != sys.argv[9]:
    problems.append("model deployment snapshot environment variable was not applied")
expected_release = str(db.get("image") or "").rsplit("@", 1)[-1]
if not expected_release or eb.get("HERMES_RELEASE_ID") != expected_release:
    problems.append("state writer release identity was not applied")
if eb.get("STATE_WRITER_PROTOCOL") != "dual-v1-v2":
    problems.append("state writer compatibility protocol was not applied")
if sys.argv[10] == "set":
    if sys.argv[11] and eb.get("MODEL_ALLOWED_DEPLOYMENTS") != sys.argv[11]:
        problems.append("model allow-list environment variable was not applied")
    if not sys.argv[11] and "MODEL_ALLOWED_DEPLOYMENTS" in eb:
        problems.append("model allow-list environment variable was not removed")
if sys.argv[12] and eb.get("M365_WRITE_POLICY") != sys.argv[12]:
    problems.append("Microsoft 365 write policy was not applied")
if sys.argv[13] and eb.get("M365_OUTBOUND_POLICY") != sys.argv[13]:
    problems.append("Microsoft 365 outbound policy was not applied")
if problems:
    sys.exit("ABORT: new version " + sys.argv[4] + " " + "; ".join(problems)
             + ". Do NOT route traffic to it.")
PY

# Leave a durable release record without copying tenant coordinates, model deployment names,
# identities, or environment values out of the guarded temporary directory. deployment-output is
# ignored by Git and is intentionally local to the deployment operator.
SOURCE_COMMIT=$(git rev-parse --verify HEAD)
if [ -z "$(git status --porcelain --untracked-files=normal)" ]; then
  SOURCE_STATE=clean
else
  SOURCE_STATE=dirty
  echo "WARNING: source worktree is dirty; the release record will mark this version as dirty."
fi
RELEASE_RECORD="deployment-output/release-records/${SOURCE_COMMIT:0:12}-v${NEW}.md"
python3 scripts/write-release-record.py \
  --output "$RELEASE_RECORD" \
  --dockerfile Dockerfile \
  --source-commit "$SOURCE_COMMIT" \
  --source-state "$SOURCE_STATE" \
  --image-digest "$DIGEST" \
  --previous-version "$CUR" \
  --new-version "$NEW"

if [[ -n "${HERMES_VERSION_RESULT_FILE:-}" ]]; then
  python3 - "$HERMES_VERSION_RESULT_FILE" "$CUR" "$NEW" "$IMAGE" <<'PYRESULT'
import sys
from pathlib import Path
sys.path.insert(0, 'scripts')
from reinstall_agent import save
save(Path(sys.argv[1]), dict(previous=sys.argv[2], version=sys.argv[3], image=sys.argv[4], verified=True))
PYRESULT
fi

cat <<EOF
==> version $NEW created and verified. NOTHING IS ROUTED TO IT YET.
Sanitized local release record: $RELEASE_RECORD

To serve it:
  ./scripts/route-agent-version.sh \\
    --account "$ACCOUNT" --project "$PROJECT" --agent "$AGENT" \\
    --target-version "$NEW" --expect-current "$CUR"

To roll back after that, reverse the target and expected-current versions. The script revalidates
the target, refuses stale state, preserves Graph permissions, and reads the route back after PATCH.
EOF
