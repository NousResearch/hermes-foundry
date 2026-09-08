#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${AZURE_SUBSCRIPTION_ID:-}" ]]; then
  az account set --subscription "$AZURE_SUBSCRIPTION_ID"
fi

providers=(
  Microsoft.CognitiveServices
  Microsoft.ContainerRegistry
  Microsoft.Logic
)

for provider in "${providers[@]}"; do
  state=$(az provider show --namespace "$provider" --query registrationState --output tsv 2>/dev/null || true)
  if [[ "$state" == "Registered" ]]; then
    printf '%s is already registered.\n' "$provider"
    continue
  fi

  printf 'Registering %s...\n' "$provider"
  az provider register --namespace "$provider" --wait --output none
done
