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
states=()

for provider in "${providers[@]}"; do
  if ! state=$(az provider show \
      --namespace "$provider" --query registrationState --output tsv); then
    printf 'ERROR: could not read the registration state for %s; no providers were registered.\n' \
      "$provider" >&2
    exit 1
  fi
  states+=("$state")
done

for index in "${!providers[@]}"; do
  provider="${providers[$index]}"
  state="${states[$index]}"
  if [[ "$state" == "Registered" ]]; then
    printf '%s is already registered.\n' "$provider"
    continue
  fi

  if [[ "${HERMES_PROVISION_MODE:-apply}" == "preview" ]]; then
    printf '%s is not registered (preview only; no change made).\n' "$provider"
    continue
  fi

  printf 'Registering %s...\n' "$provider"
  az provider register --namespace "$provider" --wait --output none
done
