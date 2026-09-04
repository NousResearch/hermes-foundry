#!/usr/bin/env bash
# Plan F entrypoint — Hermes, the edge, the gateway and the M365 sidecar in one container.
#
# STARTUP ORDER IS THE CONTRACT, learned the hard way:
#
#   1. config.yaml     Hermes reads it at boot, so it must exist first.
#   2. sidecar :8010   Hermes' MCP handshake needs it, or the agent starts with no m365 tools.
#   3. gateway :8080   Must NOT inherit the ambient PORT (see below).
#   4. edge    :8088   Binding this is what makes Foundry mark the session READY, and the platform
#                      routes a request the instant it does. Nothing slow may run before it.
#   5. Hermes          BACKGROUND. Its boot (skills sync, Playwright warm-up) exceeds the
#                      platform's readiness window, so nothing may block on it. The gateway
#                      absorbs the gap via HERMES_COLD_START_WAIT.
#
# Two earlier versions failed exactly here: one gated the edge on Hermes and lost readiness every
# time; another sat in a 60s wait loop for a process it had not started yet, so the edge bound long
# after the window closed. Readiness is ~30s observed — treat it as a hard budget.
set -uo pipefail

log() { echo "[plan-f] $*"; }

: "${MODEL_DEPLOYMENT_NAME:?MODEL_DEPLOYMENT_NAME is required}"
: "${FALLBACK_MODEL_DEPLOYMENT_NAME:?FALLBACK_MODEL_DEPLOYMENT_NAME is required}"
: "${AZURE_FOUNDRY_BASE_URL:?AZURE_FOUNDRY_BASE_URL is required}"

# The edge chooses the first currently deployed model in this administrator-defined order. Hermes'
# own fallback chain uses the same order after a selected model exhausts its normal retries. Keep
# the legacy single fallback variable as a compatibility input; MODEL_PRIORITY is the extensible
# policy and may contain any number of safe Azure deployment names.
MODEL_PRIORITY="${MODEL_PRIORITY:-${MODEL_DEPLOYMENT_NAME},${FALLBACK_MODEL_DEPLOYMENT_NAME}}"
export MODEL_PRIORITY
FALLBACK_PROVIDER_YAML=""
declare -A _seen_fallback_models=()
IFS=',' read -r -a _priority_models <<< "$MODEL_PRIORITY"
for _raw_model in "${_priority_models[@]}" "$FALLBACK_MODEL_DEPLOYMENT_NAME"; do
  _model="${_raw_model//[[:space:]]/}"
  [ -n "$_model" ] || continue
  if [[ ! "$_model" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
    log "WARNING: ignoring unsafe model deployment name in MODEL_PRIORITY"
    continue
  fi
  [ -z "${_seen_fallback_models[$_model]:-}" ] || continue
  _seen_fallback_models[$_model]=1
  FALLBACK_PROVIDER_YAML+="  - provider: \"azure-foundry\""$'\n'
  FALLBACK_PROVIDER_YAML+="    model: \"${_model}\""$'\n'
  FALLBACK_PROVIDER_YAML+="    base_url: \"${AZURE_FOUNDRY_BASE_URL}\""$'\n'
  FALLBACK_PROVIDER_YAML+="    auth_mode: \"entra_id\""$'\n'
done
unset _seen_fallback_models _priority_models _raw_model _model

# ---- shared secrets: derive, never re-declare -------------------------------------------------
# Three processes must agree on two secrets, and each one is named DIFFERENTLY at each end:
#
#   Hermes API server  API_SERVER_KEY  ==  HERMES_KEY       sent by the gateway
#   our gateway        GATEWAY_KEY     ==  AGENT_TOKEN_KEY  sent by the sidecar
#
# In the split ACA deployment those are four env vars across three container definitions, all fed
# from the same two secrets. The coupling is invisible, so it is easy to set one end and not the
# other — and both halves fail in ways that do not look like a key problem:
#
#   * API_SERVER_KEY empty -> Hermes refuses to start its API server at all ("Require a usable key:
#     API_SERVER_ENABLED alone would load an unauthenticated server"), so :8642 never binds and
#     every turn comes back as a gateway 502. Cost several deploy cycles to find.
#   * AGENT_TOKEN_KEY unset while GATEWAY_KEY is set -> the sidecar gets 401 from /v1/agent-token,
#     holds no Graph token, and EVERY m365 tool fails — while chat keeps working perfectly. That is
#     the worst failure shape available: the agent talks normally and silently cannot touch mail,
#     files or calendar.
#
# One container means one place to fix it: generate what is missing, then derive the other end from
# it. These never leave loopback, so an ephemeral per-session value is the right default.
gen_key() { head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

if [ -z "${API_SERVER_KEY:-}" ]; then
  API_SERVER_KEY="$(gen_key)"; export API_SERVER_KEY
  log "API_SERVER_KEY was empty — generated an ephemeral one (loopback only)"
fi
if [ -z "${GATEWAY_KEY:-}" ]; then
  GATEWAY_KEY="$(gen_key)"; export GATEWAY_KEY
  log "GATEWAY_KEY was empty — generated an ephemeral one (loopback only)"
fi
export HERMES_KEY="${API_SERVER_KEY}"      # gateway -> Hermes
export AGENT_TOKEN_KEY="${GATEWAY_KEY}"    # sidecar -> gateway
export RouterKey="${GATEWAY_KEY}"          # edge    -> gateway
log "keys wired: hermes=${#API_SERVER_KEY} chars, gateway=${#GATEWAY_KEY} chars"

# ---- put Hermes' state on the PERSISTED volume -------------------------------------------------
# ⚠️ `/opt/data` IS NOT PERSISTED by Foundry. The hosted-agents docs are specific about what is:
#
#   "Each session gets a dedicated sandbox with a persistent filesystem ($HOME and /files), enabling
#    scale-to-zero with stateful resume"
#   "$HOME and /files content are persisted across turns and across idle periods"
#   Idle row: "No requests for 15 minutes. Compute is deprovisioned. Session state ($HOME, /files) is
#    persisted."
#
# Plan F wrote everything Hermes owns — sessions/, memories/, MEMORY.md, USER.md, skills/, cron/ — to
# /opt/data, i.e. the container's writable layer, which is discarded on every idle teardown. The
# platform was persisting state correctly; we were storing it outside the part it keeps. So a Plan F
# agent forgot everything roughly every 15 minutes and it would have looked like Hermes' fault.
#
# HERMES_STATE is overridable only so a future non-Foundry host can point it elsewhere; the default
# must stay on a persisted path.
HERMES_STATE="${HERMES_STATE:-/files/hermes}"
mkdir -p "$HERMES_STATE" 2>/dev/null || {
  log "🔴 cannot create $HERMES_STATE — falling back to /opt/data, which is EPHEMERAL: memory and"
  log "   skills will be lost on every 15-minute idle teardown."
  HERMES_STATE=/opt/data
}
export HERMES_STATE HERMES_HOME="$HERMES_STATE" HOME="$HERMES_STATE"
log "hermes state dir: $HERMES_STATE (HOME and HERMES_HOME both point here)"
# Report whether we actually landed on a persisted path, so this is verifiable from :logstream rather
# than assumed. If /files is missing, that is a platform contract change and we want to see it.
for d in /files "$HOME"; do
  [ -d "$d" ] && log "persisted-path check: $d exists, writable=$([ -w "$d" ] && echo yes || echo NO)"
done

# _config_version is required: without it Hermes logs "This config predates version 12 and can no
# longer be auto-migrated" and does not apply the file.
mkdir -p /opt/data
cat > /tmp/hermes-config.yaml <<YAML
_config_version: 12
model:
  default: "${MODEL_DEPLOYMENT_NAME}"
  provider: "azure-foundry"
  base_url: "${AZURE_FOUNDRY_BASE_URL}"
  auth_mode: "entra_id"
fallback_providers:
${FALLBACK_PROVIDER_YAML}
agent:
  disabled_toolsets: ["file", "terminal", "code_execution", "browser", "project"]
mcp_servers:
  m365:
    url: "http://localhost:8010/mcp"
    timeout: 60
# THE API SERVER IS A GATEWAY *PLATFORM*, NOT A SERVICE — and it must be enabled here.
# The gateway logged "No messaging platforms enabled" and never bound :8642, so every turn came
# back "hermes.unreachable ... Connection refused" -> gateway 502. API_SERVER_* env vars alone are
# not enough: gateway/platforms/api_server.py reads host/port/key from \`platforms.api_server.extra\`
# and only falls back to the env vars once the platform itself is on.
platforms:
  api_server:
    enabled: true
    extra:
      host: "0.0.0.0"
      port: 8642
YAML

# WRITE BOTH PATHS. gateway/config.py documents the search order as
#   2. ~/.hermes/config.yaml   (primary user-facing config)
# and HERMES_HOME=/opt/data with HOME=/opt/data, so the file Hermes actually reads is
# /opt/data/.hermes/config.yaml. Writing only /opt/data/config.yaml (what the ACA bicep does) left
# the gateway with no platforms — "No messaging platforms enabled" — so the API server never bound
# :8642 and every turn 502'd. Write both so neither location is the one that mattered.
# WRITE INTO $HERMES_STATE, because that is what HOME now points at.
#
# This broke once, immediately: moving HOME to /files/hermes while still writing the config to
# /opt/data meant `~/.hermes/config.yaml` did not exist, and Hermes came up with no model at all —
#   "⚠️ Provider authentication failed: No inference provider configured. Run 'hermes model' ...
#    or set an API key (OPENROUTER_API_KEY, OPENAI_API_KEY, etc.) in ~/.hermes/.env."
# which reads like a missing credential and is actually a config file written to the wrong home.
mkdir -p "$HERMES_STATE/.hermes"
cp /tmp/hermes-config.yaml "$HERMES_STATE/.hermes/config.yaml"
cp /tmp/hermes-config.yaml "$HERMES_STATE/config.yaml"
# Keep /opt/data in sync too: it costs nothing and covers any code path that still hardcodes it.
mkdir -p /opt/data/.hermes
cp /tmp/hermes-config.yaml /opt/data/.hermes/config.yaml
cp /tmp/hermes-config.yaml /opt/data/config.yaml
log "wrote config.yaml to $HERMES_STATE/.hermes/, $HERMES_STATE/ and /opt/data/ (primary=$MODEL_DEPLOYMENT_NAME priority=$MODEL_PRIORITY)"
log "config sanity: $(head -c 120 "$HERMES_STATE/.hermes/config.yaml" | tr '\n' ' ')"

# Respawn wrapper. Output goes to the container's STDOUT, prefixed — redirecting to files hid the
# edge's bind exception from :logstream for several deploy cycles, and :logstream is the only view
# into a hosted-agent sandbox.
supervise() {
  local name="$1"; shift
  ( while true; do
      log "starting $name"
      "$@" 2>&1 | sed -u "s/^/[$name] /"
      log "$name exited rc=$? — restarting in 3s"
      log "$name listeners: $( (ss -lntp 2>/dev/null || echo 'no ss') | tr '\n' ' ' | cut -c1-350)"
      sleep 3
    done ) &
}

# ⚠️ PORT=8088 is injected by the platform — it is the port the AGENT must serve on, and the agent
# is the edge. edge-gateway/app.py reads os.environ["PORT"], so in a single container it grabbed
# 8088 first and the edge died with "address already in use". Pin every port explicitly.
# The sidecar runs on its OWN venv interpreter, never the ambient `python3` — that is Hermes' venv,
# which does not have our pins and produced `ModuleNotFoundError: mcp.server.fastmcp`.
supervise sidecar /opt/sidecar-venv/bin/python /opt/sidecar/agent365_mcp.py
supervise gateway env PORT=8080 python3 /opt/gateway/app.py

# The edge — start it NOW so readiness passes. WORKDIR matters: Microsoft's hosted-agent sample is
# `WORKDIR /app` then `dotnet <app>.dll`, so ASP.NET finds appsettings.json beside the dll.
#
# ⚠️ NOTHING SLOW MAY GO ABOVE THIS LINE. A 20s "wait for the sidecar" loop used to sit here, which
# spent up to two thirds of the ~30s readiness budget before the edge even started.
supervise edge env -C /opt/edge ASPNETCORE_URLS=http://+:8088 dotnet /opt/edge/HelloWorldA365Agent.dll

# NOW wait for the sidecar — after readiness is safe, and before Hermes.
#
# Hermes connects its MCP servers ONCE, at startup. If :8010 is not listening by then, Hermes comes
# up with no m365 server and stays that way for the whole life of the session: no mail, no calendar,
# no files, no documents. Chat works perfectly, so it looks healthy. The tell is the tool list —
# `tool_search`/`tool_describe`/`tool_call` are present only when an MCP server attached, so 10 tools
# means broken and 13 means wired.
#
# This is exactly what happened on hfull's first-ever cold start (2026-08-17): the old loop waited
# only 20s and then fell through SILENTLY, and the sidecar — which imports playwright, python-docx
# and openpyxl — had not finished binding while the container was also serving a 1 GiB image pull.
# So: wait generously, and be loud either way. Nothing gates on Hermes, so waiting here is free.
SIDECAR_WAIT="${SIDECAR_WAIT:-180}"
for i in $(seq 1 "$SIDECAR_WAIT"); do
  (echo > /dev/tcp/127.0.0.1/8010) >/dev/null 2>&1 && { log "sidecar listening after ${i}s"; break; }
  [ "$i" = "$SIDECAR_WAIT" ] && log "🔴 WARNING: sidecar did NOT bind :8010 in ${SIDECAR_WAIT}s — Hermes \
will start with NO m365 tools (mail/calendar/files/docs all unavailable) for this whole session. \
Chat will still work, which is what makes this easy to miss. Check the [sidecar] lines above."
  sleep 1
done

# Hermes last, in the background, via the image's OWN dispatcher — not /init, not main-wrapper.sh.
# entrypoint-dispatch.sh exists for runtimes that don't give the image PID 1; its comment names
# "Fly Machines, docker run --init, some Nomad/K8s setups", which is exactly this runtime. It also
# exports PATH=/command:/package/admin/s6/command — calling the wrapper directly failed with
# "exec: s6-setuidgid: not found" because s6's helpers are not on the image PATH.
log "starting Hermes in background via entrypoint-dispatch.sh"
( while true; do
    /opt/hermes/docker/entrypoint-dispatch.sh gateway run 2>&1 | tee -a /tmp/hermes.log | sed -u 's/^/[hermes] /'
    log "hermes exited rc=$? — restarting in 3s"
    sleep 3
  done ) &

# Report, never block — these make cold start measurable without delaying readiness.
( for i in $(seq 1 120); do
    ss -lntp 2>/dev/null | grep -q ':8088 .*dotnet' && { log "edge owns :8088 after ${i}s — READY"; exit 0; }
    sleep 1
  done; log "WARNING: edge never bound :8088 — /readiness will fail" ) &

( for i in $(seq 1 300); do
    (echo > /dev/tcp/127.0.0.1/8642) >/dev/null 2>&1 && { log "hermes API up on :8642 after ${i}s"; exit 0; }
    sleep 1
  done; log "WARNING: hermes never bound :8642 in 300s — gateway will 502 after HERMES_COLD_START_WAIT" ) &

while true; do sleep 3600; done
