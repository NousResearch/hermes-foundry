#!/usr/bin/env bash
# Read-only Azure prerequisite check. This script never registers a provider, assigns a role,
# creates a resource, or changes the active subscription.
set -euo pipefail

usage() {
  printf '%s\n' \
    "Usage: ./scripts/validate-installation.sh --tenant <tenant-id> --subscription <subscription-id> [--location <region>]"
}

TENANT_ID=""
SUBSCRIPTION_ID=""
LOCATION=""
while (($#)); do
  case "$1" in
    --tenant) TENANT_ID="${2:?}"; shift 2 ;;
    --subscription) SUBSCRIPTION_ID="${2:?}"; shift 2 ;;
    --location) LOCATION="${2:?}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ "$TENANT_ID" =~ ^[0-9A-Fa-f-]{36}$ ]] || {
  printf 'ERROR: --tenant must be a tenant GUID.\n' >&2
  exit 2
}
[[ "$SUBSCRIPTION_ID" =~ ^[0-9A-Fa-f-]{36}$ ]] || {
  printf 'ERROR: --subscription must be a subscription GUID.\n' >&2
  exit 2
}
command -v az >/dev/null || { printf 'ERROR: Azure CLI (az) is required.\n' >&2; exit 2; }
command -v azd >/dev/null || { printf 'ERROR: Azure Developer CLI (azd) is required.\n' >&2; exit 2; }

errors=0
pass() { printf 'PASS: %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; errors=$((errors + 1)); }

active_tenant=$(az account show --query tenantId --output tsv 2>/dev/null || true)
active_subscription=$(az account show --query id --output tsv 2>/dev/null || true)
active_tenant_lower=$(printf '%s' "$active_tenant" | tr '[:upper:]' '[:lower:]')
tenant_id_lower=$(printf '%s' "$TENANT_ID" | tr '[:upper:]' '[:lower:]')
active_subscription_lower=$(printf '%s' "$active_subscription" | tr '[:upper:]' '[:lower:]')
subscription_id_lower=$(printf '%s' "$SUBSCRIPTION_ID" | tr '[:upper:]' '[:lower:]')
[[ "$active_tenant_lower" == "$tenant_id_lower" ]] \
  && pass "Azure CLI is using the selected tenant." \
  || fail "Azure CLI is not using the selected tenant."
[[ "$active_subscription_lower" == "$subscription_id_lower" ]] \
  && pass "Azure CLI is using the selected subscription." \
  || fail "Azure CLI is not using the selected subscription."

if az account get-access-token --tenant "$TENANT_ID" \
    --resource "https://management.azure.com/" --output none >/dev/null 2>&1; then
  pass "The administrator can obtain an Azure management token."
else
  fail "The administrator cannot obtain an Azure management token."
fi

if az account get-access-token --tenant "$TENANT_ID" \
    --resource "https://ai.azure.com" --output none >/dev/null 2>&1; then
  pass "The administrator can obtain a Microsoft Foundry data-plane token."
else
  fail "The administrator cannot obtain a Microsoft Foundry data-plane token."
fi

for provider in Microsoft.CognitiveServices Microsoft.ContainerRegistry Microsoft.Logic; do
  state=$(az provider show --namespace "$provider" --query registrationState \
    --output tsv 2>/dev/null || true)
  if [[ "$state" == "Registered" ]]; then
    pass "$provider is registered."
  else
    # Apply mode can register these providers, but validation must remain read-only and should make
    # the pending administrator action explicit.
    fail "$provider is not registered (current state: ${state:-unknown})."
  fi
done

if [[ -n "$LOCATION" ]]; then
  normalized_location=$(printf '%s' "$LOCATION" | tr '[:upper:]' '[:lower:]' | tr -d ' ')
  if az account list-locations --query "[?name=='${normalized_location}'].name | [0]" \
      --output tsv 2>/dev/null | grep -qx "$normalized_location"; then
    pass "Azure location $normalized_location exists for this subscription."
  else
    fail "Azure location $LOCATION is not available to this subscription."
  fi
else
  printf 'INFO: no AZURE_LOCATION was supplied; regional model/hosted-agent availability was not checked.\n'
fi

if az bicep version >/dev/null 2>&1; then
  pass "Azure CLI Bicep support is installed."
else
  fail "Azure CLI Bicep support is unavailable."
fi

if azd auth token --tenant-id "$TENANT_ID" \
    --scope "https://management.azure.com/.default" >/dev/null 2>&1; then
  pass "Azure Developer CLI authentication matches the selected tenant."
else
  fail "Azure Developer CLI cannot obtain a token for the selected tenant."
fi

printf '%s\n' \
  "INFO: Microsoft 365 agent licensing, blueprint approval, hiring scope, and workload provisioning" \
  "      are Microsoft 365 control-plane checks and cannot be proven by this Azure-only preflight."

if ((errors)); then
  printf 'Preflight failed with %d issue(s). No resources were changed.\n' "$errors" >&2
  exit 1
fi

printf 'Preflight passed. No resources were changed.\n'
